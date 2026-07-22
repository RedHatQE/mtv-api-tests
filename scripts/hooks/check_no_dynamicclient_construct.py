#!/usr/bin/env python3
"""Ban constructing DynamicClient directly; use get_client() instead."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from _ast_utils import (
    FindingCollector,
    for_each_parsed_file,
    main_runner,
    walk_with_stack,
)

_DYNAMIC_CLIENT_MODULES: frozenset[str] = frozenset({
    "kubernetes.dynamic",
    "kubernetes.dynamic.client",
})


def _collect_dynamic_client_bindings(
    tree: ast.AST,
) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    """Collect names that resolve to kubernetes ``DynamicClient`` constructions.

    Scans module imports once and returns:

    - class names from ``from kubernetes.dynamic[.client] import DynamicClient [as X]``
    - module aliases from ``import kubernetes.dynamic[.client] as X``,
      ``from kubernetes import dynamic [as X]``, and
      ``from kubernetes.dynamic import client [as X]``
    - package aliases from ``import kubernetes [as X]`` (and dotted imports that
      bind the top-level ``kubernetes`` package)

    Args:
        tree (ast.AST): Parsed module AST.

    Returns:
        tuple[frozenset[str], frozenset[str], frozenset[str]]:
        ``(class_names, module_aliases, package_aliases)``.
    """
    class_names: set[str] = set()
    module_aliases: set[str] = set()
    package_aliases: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                if name in _DYNAMIC_CLIENT_MODULES:
                    if alias.asname:
                        module_aliases.add(alias.asname)
                    else:
                        # ``import kubernetes.dynamic`` binds the top-level package.
                        package_aliases.add("kubernetes")
                elif name == "kubernetes":
                    package_aliases.add(alias.asname or "kubernetes")
                elif name.startswith("kubernetes.") and alias.asname is None:
                    # Dotted imports without ``as`` bind the top-level package.
                    package_aliases.add("kubernetes")
        elif isinstance(node, ast.ImportFrom):
            if node.module in _DYNAMIC_CLIENT_MODULES:
                for alias in node.names:
                    if alias.name == "DynamicClient":
                        class_names.add(alias.asname or "DynamicClient")
                    elif node.module == "kubernetes.dynamic" and alias.name == "client":
                        module_aliases.add(alias.asname or "client")
            elif node.module == "kubernetes":
                for alias in node.names:
                    if alias.name == "dynamic":
                        module_aliases.add(alias.asname or "dynamic")

    return frozenset(class_names), frozenset(module_aliases), frozenset(package_aliases)


def _attr_chain(func: ast.expr) -> list[str] | None:
    """Return the dotted name parts of ``func`` if it is a Name/Attribute chain.

    Args:
        func (ast.expr): Call callee expression.

    Returns:
        list[str] | None: Name parts from left to right, or None if not a
        simple attribute chain.
    """
    parts: list[str] = []
    cur: ast.expr = func
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if not isinstance(cur, ast.Name):
        return None
    parts.append(cur.id)
    parts.reverse()
    return parts


def _is_kubernetes_dynamic_client_call(
    func: ast.expr,
    class_names: frozenset[str],
    module_aliases: frozenset[str],
    package_aliases: frozenset[str],
) -> bool:
    """Return True if ``func`` constructs kubernetes ``DynamicClient``.

    Recognizes only callees that resolve via collected imports:

    - bare / aliased class: ``DynamicClient(...)``, ``DC(...)``
    - module alias: ``kd.DynamicClient(...)``, ``kdc.DynamicClient(...)``
    - dynamic-module alias nested: ``kd.client.DynamicClient(...)`` when
      ``kd`` aliases ``kubernetes.dynamic``
    - package nested: ``kubernetes.dynamic.DynamicClient(...)``,
      ``k8s.dynamic.client.DynamicClient(...)``

    Args:
        func (ast.expr): The callee expression of a Call node.
        class_names (frozenset[str]): Names bound to DynamicClient.
        module_aliases (frozenset[str]): Aliases of kubernetes.dynamic[.client].
        package_aliases (frozenset[str]): Aliases of the kubernetes package.

    Returns:
        bool: Whether the callee is a forbidden DynamicClient construction.
    """
    if isinstance(func, ast.Name):
        return func.id in class_names

    parts = _attr_chain(func)
    if parts is None or parts[-1] != "DynamicClient":
        return False

    # ``kd.DynamicClient`` / ``kdc.DynamicClient``
    if len(parts) == 2 and parts[0] in module_aliases:
        return True

    # ``kd.client.DynamicClient`` when ``kd`` aliases ``kubernetes.dynamic``
    if len(parts) == 3 and parts[0] in module_aliases and parts[1] == "client":
        return True

    # ``kubernetes.dynamic.DynamicClient`` / ``k8s.dynamic.DynamicClient``
    if len(parts) == 3 and parts[0] in package_aliases and parts[1] == "dynamic":
        return True

    # ``kubernetes.dynamic.client.DynamicClient`` / ``k8s.dynamic.client.DynamicClient``
    return len(parts) == 4 and parts[0] in package_aliases and parts[1] == "dynamic" and parts[2] == "client"


def check_file(path: Path, tree: ast.AST, collector: FindingCollector) -> None:
    """Flag kubernetes DynamicClient(...) constructions in one file.

    Args:
        path (Path): Source file path.
        tree (ast.AST): Parsed AST.
        collector (FindingCollector): Finding sink.
    """
    class_names, module_aliases, package_aliases = _collect_dynamic_client_bindings(tree)
    if not class_names and not module_aliases and not package_aliases:
        return

    for node, _stack in walk_with_stack(tree):
        if not isinstance(node, ast.Call):
            continue
        if _is_kubernetes_dynamic_client_call(
            node.func,
            class_names,
            module_aliases,
            package_aliases,
        ):
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
    for_each_parsed_file(paths, collector, check_file)


if __name__ == "__main__":
    sys.exit(main_runner(run_check))
