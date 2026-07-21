#!/usr/bin/env python3
"""Allow only the autouse_fixtures fixture in conftest.py to use autouse=True."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from _ast_utils import FindingCollector, iter_py_files, main_runner, parse_file, walk_with_stack

_ALLOWED_FIXTURE_NAME = "autouse_fixtures"
_ALLOWED_FILENAME = "conftest.py"


def _decorator_is_fixture(dec: ast.expr) -> bool:
    """Return True if ``dec`` is a pytest fixture decorator (bare or call).

    Args:
        dec (ast.expr): Decorator expression.

    Returns:
        bool: Whether this is ``fixture`` / ``pytest.fixture`` (with or without call).
    """
    target = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(target, ast.Name) and target.id == "fixture":
        return True
    return (
        isinstance(target, ast.Attribute)
        and target.attr == "fixture"
        and isinstance(target.value, ast.Name)
        and target.value.id == "pytest"
    )


def _fixture_has_autouse_true(dec: ast.expr) -> bool:
    """Return True if a fixture decorator call sets ``autouse=True``.

    Args:
        dec (ast.expr): Decorator expression.

    Returns:
        bool: Whether autouse=True is present.
    """
    if not isinstance(dec, ast.Call):
        return False
    for keyword in dec.keywords:
        if keyword.arg != "autouse":
            continue
        return isinstance(keyword.value, ast.Constant) and keyword.value.value is True
    return False


def _is_allowlisted(path: Path, func_name: str) -> bool:
    """Return True if this is ``autouse_fixtures`` defined in ``conftest.py``.

    Args:
        path (Path): Source file path.
        func_name (str): Fixture function name.

    Returns:
        bool: Whether this autouse fixture is allowlisted.
    """
    return func_name == _ALLOWED_FIXTURE_NAME and path.name == _ALLOWED_FILENAME


def check_file(path: Path, tree: ast.AST, collector: FindingCollector) -> None:
    """Flag autouse fixtures that are not autouse_fixtures in conftest.py.

    Args:
        path (Path): Source file path.
        tree (ast.AST): Parsed AST.
        collector (FindingCollector): Finding sink.
    """
    for node, _stack in walk_with_stack(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        has_autouse = False
        for dec in node.decorator_list:
            if _decorator_is_fixture(dec) and _fixture_has_autouse_true(dec):
                has_autouse = True
                break

        if not has_autouse:
            continue

        if _is_allowlisted(path, node.name):
            continue

        collector.report(
            path,
            node.lineno,
            f"autouse=True is only allowed on fixture '{_ALLOWED_FIXTURE_NAME}' "
            f"in {_ALLOWED_FILENAME}, found '{node.name}' in {path.name}",
        )


def run_check(paths: list[str], collector: FindingCollector) -> None:
    """Run the autouse fixture name check on the given paths.

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
