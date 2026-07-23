#!/usr/bin/env python3
"""Allow only the autouse_fixtures fixture in conftest.py to enable autouse."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from _ast_utils import FindingCollector, for_each_parsed_file, main_runner, walk_with_stack
from baseline import repo_relative_posix

_ALLOWED_FIXTURE_NAME = "autouse_fixtures"
_ALLOWED_FILENAME = "conftest.py"


def _collect_fixture_aliases(tree: ast.AST) -> tuple[frozenset[str], frozenset[str]]:
    """Collect pytest module and fixture name aliases from module-level imports.

    Only scans ``Import`` / ``ImportFrom`` nodes in ``tree.body`` (module
    scope). Nested imports inside functions or classes are ignored so
    function-local aliases cannot broaden detection incorrectly.

    Always includes ``pytest`` and ``fixture`` so bare ``@pytest.fixture`` /
    ``@fixture`` keep working even when the import is not scanned (e.g. star
    imports). Also records:

    - ``import pytest as <alias>`` → ``<alias>.fixture``
    - ``from pytest import fixture`` / ``from pytest import fixture as <alias>``

    Args:
        tree (ast.AST): Parsed module AST.

    Returns:
        tuple[frozenset[str], frozenset[str]]:
        ``(pytest_module_aliases, fixture_name_aliases)``.
    """
    pytest_aliases: set[str] = {"pytest"}
    fixture_aliases: set[str] = {"fixture"}
    body = getattr(tree, "body", ())
    for node in body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "pytest":
                    pytest_aliases.add(alias.asname or "pytest")
        elif isinstance(node, ast.ImportFrom) and node.module == "pytest":
            for alias in node.names:
                if alias.name == "fixture":
                    fixture_aliases.add(alias.asname or "fixture")
    return frozenset(pytest_aliases), frozenset(fixture_aliases)


def _decorator_is_fixture(
    dec: ast.expr,
    pytest_aliases: frozenset[str],
    fixture_aliases: frozenset[str],
) -> bool:
    """Return True if ``dec`` is a pytest fixture decorator (bare or call).

    Recognizes ``@fixture`` / ``@<fixture_alias>``, ``@pytest.fixture``, and
    ``@<pytest_alias>.fixture``, with or without a call.

    Args:
        dec (ast.expr): Decorator expression.
        pytest_aliases (frozenset[str]): Names bound to the pytest module.
        fixture_aliases (frozenset[str]): Names bound to ``pytest.fixture``.

    Returns:
        bool: Whether this is a fixture decorator (with or without call).
    """
    target = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(target, ast.Name) and target.id in fixture_aliases:
        return True
    return (
        isinstance(target, ast.Attribute)
        and target.attr == "fixture"
        and isinstance(target.value, ast.Name)
        and target.value.id in pytest_aliases
    )


def _fixture_has_autouse_enabled(dec: ast.expr) -> bool:
    """Return True if a fixture decorator call enables ``autouse``.

    Flags ``autouse=<truthy Constant>`` (``True``, ``1``, ``"yes"``, etc.) and
    any non-Constant ``autouse=`` expression (treated as a potential enable).
    Also fail-closed on ``**kwargs`` (``keyword.arg is None``). Does not flag
    ``autouse=False``, ``autouse=0``, or other falsy constants, and does not
    flag a bare ``@fixture`` / ``@pytest.fixture`` with no ``autouse`` keyword.

    Args:
        dec (ast.expr): Decorator expression.

    Returns:
        bool: Whether autouse appears enabled (or potentially enabled).
    """
    if not isinstance(dec, ast.Call):
        return False
    for keyword in dec.keywords:
        # ``**kwargs`` / bare ``**`` — fail closed (treat as potentially enabled)
        if keyword.arg is None:
            return True
        if keyword.arg != "autouse":
            continue
        value = keyword.value
        if isinstance(value, ast.Constant):
            return bool(value.value)
        return True
    return False


def _is_allowlisted(path: Path, func_name: str) -> bool:
    """Return True if this is ``autouse_fixtures`` in repo-root ``conftest.py``.

    Nested ``conftest.py`` files are not allowlisted — only the exact
    repository-root path ``conftest.py`` qualifies.

    Args:
        path (Path): Source file path.
        func_name (str): Fixture function name.

    Returns:
        bool: Whether this autouse fixture is allowlisted.
    """
    return func_name == _ALLOWED_FIXTURE_NAME and repo_relative_posix(path) == _ALLOWED_FILENAME


def _check_file(path: Path, tree: ast.AST, collector: FindingCollector) -> None:
    """Flag enabled autouse fixtures that are not autouse_fixtures in conftest.py.

    Args:
        path (Path): Source file path.
        tree (ast.AST): Parsed AST.
        collector (FindingCollector): Finding sink.
    """
    pytest_aliases, fixture_aliases = _collect_fixture_aliases(tree)
    for node, _stack in walk_with_stack(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        has_autouse = False
        for dec in node.decorator_list:
            if _decorator_is_fixture(dec, pytest_aliases, fixture_aliases) and _fixture_has_autouse_enabled(dec):
                has_autouse = True
                break

        if not has_autouse:
            continue

        if _is_allowlisted(path, node.name):
            continue

        collector.report(
            path,
            node.lineno,
            f"autouse is only allowed on fixture '{_ALLOWED_FIXTURE_NAME}' "
            f"in {_ALLOWED_FILENAME}, found '{node.name}' in {path.name}",
        )


def run_check(paths: list[str], collector: FindingCollector) -> None:
    """Run the autouse fixture name check on the given paths.

    Args:
        paths (list[str]): File paths from pre-commit (empty = whole repo).
        collector (FindingCollector): Finding sink.
    """
    for_each_parsed_file(paths, collector, _check_file)


if __name__ == "__main__":
    sys.exit(main_runner(run_check))
