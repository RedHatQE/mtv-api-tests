#!/usr/bin/env python3
"""Ban runtime kubernetes package imports except allowed exceptions forms.

Allowed outside TYPE_CHECKING:

- ``import kubernetes.dynamic.exceptions`` / ``from kubernetes.dynamic.exceptions import ...``
- ``from kubernetes.dynamic import exceptions``
"""

from __future__ import annotations

import ast
import sys
from collections import Counter
from functools import partial
from pathlib import Path

from _ast_utils import (
    FindingCollector,
    collect_typing_aliases,
    for_each_parsed_file,
    is_type_checking_test,
    main_runner,
    walk_with_stack,
)
from baseline import is_baselined, load_baseline_for_check


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


def _inside_type_checking(
    stack: list[ast.AST],
    node: ast.AST,
    typing_aliases: frozenset[str],
    type_checking_names: frozenset[str],
) -> bool:
    """Return True if ``node`` is under ``If.body`` of a TYPE_CHECKING if.

    Membership in ``If.orelse`` (the ``else`` / ``elif`` branch) does not
    count as TYPE_CHECKING-guarded.

    Args:
        stack (list[ast.AST]): Ancestor nodes from root to parent.
        node (ast.AST): Current node being inspected.
        typing_aliases (frozenset[str]): Names bound to the typing module.
        type_checking_names (frozenset[str]): Names bound to TYPE_CHECKING.

    Returns:
        bool: Whether the current node is under a TYPE_CHECKING ``If.body``.
    """
    for i, ancestor in enumerate(stack):
        if not isinstance(ancestor, ast.If) or not is_type_checking_test(
            ancestor.test,
            typing_aliases=typing_aliases,
            type_checking_names=type_checking_names,
        ):
            continue
        child = stack[i + 1] if i + 1 < len(stack) else node
        if child in ancestor.body:
            return True
    return False


def _check_file(
    path: Path,
    tree: ast.AST,
    collector: FindingCollector,
    baseline: Counter[tuple[str, str]],
) -> None:
    """Flag forbidden runtime kubernetes imports in one file.

    Args:
        path (Path): Source file path.
        tree (ast.AST): Parsed AST.
        collector (FindingCollector): Finding sink.
        baseline (Counter[tuple[str, str]]): Grandfathered path/fingerprint
            occurrence counts.
    """
    typing_aliases, type_checking_names = collect_typing_aliases(tree)
    for node, stack in walk_with_stack(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue

        if _inside_type_checking(stack, node, typing_aliases, type_checking_names):
            continue

        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _is_kubernetes_module(alias.name):
                    continue
                if _is_allowed_exceptions_module(alias.name):
                    continue
                if is_baselined(path, node.lineno, baseline, end_lineno=node.end_lineno):
                    continue
                collector.report(
                    path,
                    node.lineno,
                    f"forbidden runtime import of '{alias.name}' "
                    "(only kubernetes.dynamic.exceptions and "
                    "'from kubernetes.dynamic import exceptions' are allowed "
                    "outside TYPE_CHECKING)",
                )
            continue

        if node.module is None or not _is_kubernetes_module(node.module):
            continue
        if _is_allowed_exceptions_module(node.module):
            continue
        if _is_allowed_dynamic_exceptions_from(node):
            continue
        if is_baselined(path, node.lineno, baseline, end_lineno=node.end_lineno):
            continue
        collector.report(
            path,
            node.lineno,
            f"forbidden runtime import from '{node.module}' "
            "(only kubernetes.dynamic.exceptions and "
            "'from kubernetes.dynamic import exceptions' are allowed "
            "outside TYPE_CHECKING)",
        )


def run_check(paths: list[str], collector: FindingCollector) -> None:
    """Run the kubernetes runtime import check on the given paths.

    Args:
        paths (list[str]): File paths from pre-commit (empty = whole repo).
        collector (FindingCollector): Finding sink.
    """
    baseline = load_baseline_for_check("check_no_kubernetes_runtime", collector.report)
    if baseline is None:
        return

    for_each_parsed_file(paths, collector, partial(_check_file, baseline=baseline))


if __name__ == "__main__":
    sys.exit(main_runner(run_check))
