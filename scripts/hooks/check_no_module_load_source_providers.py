#!/usr/bin/env python3
"""Ban module-level load_source_providers() calls under tests/."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from _ast_utils import (
    FindingCollector,
    for_each_parsed_file,
    is_module_level,
    main_runner,
    walk_with_stack,
)


def _is_under_tests(path: Path) -> bool:
    """Return True if ``path`` is under a ``tests/`` directory.

    Args:
        path (Path): Source file path.

    Returns:
        bool: Whether the path is inside tests/.
    """
    return "tests" in path.parts


def _is_load_source_providers_call(node: ast.Call) -> bool:
    """Return True if ``node`` calls ``load_source_providers``.

    Args:
        node (ast.Call): Call AST node.

    Returns:
        bool: Whether the callee is load_source_providers.
    """
    func = node.func
    if isinstance(func, ast.Name) and func.id == "load_source_providers":
        return True
    return isinstance(func, ast.Attribute) and func.attr == "load_source_providers"


def check_file(path: Path, tree: ast.AST, collector: FindingCollector) -> None:
    """Flag module-level load_source_providers calls in one tests/ file.

    Args:
        path (Path): Source file path.
        tree (ast.AST): Parsed AST.
        collector (FindingCollector): Finding sink.
    """
    if not _is_under_tests(path):
        return

    for node, stack in walk_with_stack(tree):
        if not isinstance(node, ast.Call):
            continue
        if not _is_load_source_providers_call(node):
            continue
        if not is_module_level(stack):
            continue
        collector.report(
            path,
            node.lineno,
            "forbidden module-level load_source_providers(); call only inside fixtures/hooks",
        )


def run_check(paths: list[str], collector: FindingCollector) -> None:
    """Run the module-level load_source_providers check on tests/ files.

    Args:
        paths (list[str]): File paths from pre-commit (empty = whole tests/).
        collector (FindingCollector): Finding sink.
    """
    for_each_parsed_file(paths if paths else ["tests"], collector, check_file)


if __name__ == "__main__":
    sys.exit(main_runner(run_check))
