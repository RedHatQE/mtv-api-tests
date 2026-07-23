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

_ScopeKey = ast.AST | None  # None = module scope
_BindingSets = tuple[set[str], set[str], set[str]]  # class, module, package


def _enclosing_scope_key(stack: list[ast.AST]) -> _ScopeKey:
    """Return nearest FunctionDef/AsyncFunctionDef/ClassDef, or None for module.

    Args:
        stack (list[ast.AST]): Ancestors from root to parent of current node.

    Returns:
        ast.AST | None: Enclosing scope node, or None for module scope.
    """
    for node in reversed(stack):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return node
    return None


def _record_import_bindings(node: ast.Import | ast.ImportFrom, bindings: _BindingSets) -> None:
    """Add DynamicClient-related names from one import statement into ``bindings``.

    Args:
        node (ast.Import | ast.ImportFrom): Import statement.
        bindings (_BindingSets): ``(class_names, module_aliases, package_aliases)``.
    """
    class_names, module_aliases, package_aliases = bindings
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
        return

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


def _collect_scoped_bindings(
    tree: ast.AST,
) -> dict[_ScopeKey, tuple[frozenset[str], frozenset[str], frozenset[str]]]:
    """Collect DynamicClient import bindings keyed by enclosing scope.

    Module-scope imports are under key ``None``. Nested imports inside
    functions/classes are keyed by that scope node so they do not affect
    call matching in other scopes.

    Args:
        tree (ast.AST): Parsed module AST.

    Returns:
        dict: Scope key → ``(class_names, module_aliases, package_aliases)``.
    """
    mutable: dict[_ScopeKey, _BindingSets] = {}

    for node, stack in walk_with_stack(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        scope = _enclosing_scope_key(stack)
        if scope not in mutable:
            mutable[scope] = (set(), set(), set())
        _record_import_bindings(node, mutable[scope])

    return {
        scope: (frozenset(classes), frozenset(modules), frozenset(packages))
        for scope, (classes, modules, packages) in mutable.items()
    }


def _bindings_for_call(
    scope: _ScopeKey,
    by_scope: dict[_ScopeKey, tuple[frozenset[str], frozenset[str], frozenset[str]]],
) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    """Union module-scope bindings with the call's enclosing-scope bindings.

    Args:
        scope (_ScopeKey): Enclosing scope of the Call (None = module).
        by_scope: Scoped binding map from ``_collect_scoped_bindings``.

    Returns:
        tuple[frozenset[str], frozenset[str], frozenset[str]]:
        ``(class_names, module_aliases, package_aliases)`` visible at the call.
    """
    empty: frozenset[str] = frozenset()
    mod_classes, mod_modules, mod_packages = by_scope.get(None, (empty, empty, empty))
    if scope is None:
        return mod_classes, mod_modules, mod_packages

    loc_classes, loc_modules, loc_packages = by_scope.get(scope, (empty, empty, empty))
    return (
        mod_classes | loc_classes,
        mod_modules | loc_modules,
        mod_packages | loc_packages,
    )


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


def _check_file(path: Path, tree: ast.AST, collector: FindingCollector) -> None:
    """Flag kubernetes DynamicClient(...) constructions in one file.

    Args:
        path (Path): Source file path.
        tree (ast.AST): Parsed AST.
        collector (FindingCollector): Finding sink.
    """
    by_scope = _collect_scoped_bindings(tree)
    if not by_scope:
        return

    for node, stack in walk_with_stack(tree):
        if not isinstance(node, ast.Call):
            continue
        class_names, module_aliases, package_aliases = _bindings_for_call(
            _enclosing_scope_key(stack),
            by_scope,
        )
        if not class_names and not module_aliases and not package_aliases:
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
    for_each_parsed_file(paths, collector, _check_file)


if __name__ == "__main__":
    sys.exit(main_runner(run_check))
