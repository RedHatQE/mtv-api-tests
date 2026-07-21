#!/usr/bin/env python3
"""Ban runtime kubernetes package imports except kubernetes.dynamic.exceptions."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from _ast_utils import (
    FindingCollector,
    for_each_parsed_file,
    is_type_checking_test,
    main_runner,
    walk_with_stack,
)
from baseline import is_baselined, load_baseline

_BASELINE = load_baseline("check_no_kubernetes_runtime")


def _is_allowed_exceptions_module(module: str) -> bool:
    """Return True for ``kubernetes.dynamic.exceptions`` module paths.

    Args:
        module (str): Import module path (``from X import`` or ``import X``).

    Returns:
        bool: Whether this import is the allowed exceptions module.
    """
    return module == "kubernetes.dynamic.exceptions" or module.startswith("kubernetes.dynamic.exceptions.")


def _is_allowed_dynamic_exceptions_from(node: ast.ImportFrom) -> bool:
    """Return True for ``from kubernetes.dynamic import exceptions`` (only that name).

    Mixed imports that include any name other than ``exceptions`` are not
    allowed (e.g. ``from kubernetes.dynamic import exceptions, DynamicClient``).

    Args:
        node (ast.ImportFrom): Import-from AST node.

    Returns:
        bool: Whether this import is the allowed dynamic.exceptions form.
    """
    if node.module != "kubernetes.dynamic":
        return False
    return bool(node.names) and all(alias.name == "exceptions" for alias in node.names)


def _is_kubernetes_module(module: str) -> bool:
    """Return True if ``module`` is ``kubernetes`` or a submodule.

    Args:
        module (str): Dotted module name.

    Returns:
        bool: Whether the module is under kubernetes.
    """
    return module == "kubernetes" or module.startswith("kubernetes.")


def _inside_type_checking(stack: list[ast.AST]) -> bool:
    """Return True if any ancestor is ``if TYPE_CHECKING:``.

    Args:
        stack (list[ast.AST]): Ancestor nodes from root to parent.

    Returns:
        bool: Whether the current node is under a TYPE_CHECKING guard.
    """
    for ancestor in stack:
        if isinstance(ancestor, ast.If) and is_type_checking_test(ancestor.test):
            return True
    return False


def check_file(path: Path, tree: ast.AST, collector: FindingCollector) -> None:
    """Flag forbidden runtime kubernetes imports in one file.

    Args:
        path (Path): Source file path.
        tree (ast.AST): Parsed AST.
        collector (FindingCollector): Finding sink.
    """
    for node, stack in walk_with_stack(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue

        if _inside_type_checking(stack):
            continue

        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _is_kubernetes_module(alias.name):
                    continue
                if _is_allowed_exceptions_module(alias.name):
                    continue
                if is_baselined(path, node.lineno, _BASELINE):
                    continue
                collector.report(
                    path,
                    node.lineno,
                    f"forbidden runtime import of '{alias.name}' "
                    "(only kubernetes.dynamic.exceptions is allowed outside TYPE_CHECKING)",
                )
            continue

        if node.module is None or not _is_kubernetes_module(node.module):
            continue
        if _is_allowed_exceptions_module(node.module):
            continue
        if _is_allowed_dynamic_exceptions_from(node):
            continue
        if is_baselined(path, node.lineno, _BASELINE):
            continue
        collector.report(
            path,
            node.lineno,
            f"forbidden runtime import from '{node.module}' "
            "(only kubernetes.dynamic.exceptions is allowed outside TYPE_CHECKING)",
        )


def run_check(paths: list[str], collector: FindingCollector) -> None:
    """Run the kubernetes runtime import check on the given paths.

    Args:
        paths (list[str]): File paths from pre-commit (empty = whole repo).
        collector (FindingCollector): Finding sink.
    """
    for_each_parsed_file(paths, collector, check_file)


if __name__ == "__main__":
    sys.exit(main_runner(run_check))
