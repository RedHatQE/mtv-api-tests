#!/usr/bin/env python3
"""Ban bare ``except Exception`` outside pytest hooks in conftest.py."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from _ast_utils import (
    FindingCollector,
    for_each_parsed_file,
    is_pytest_hook_in_conftest,
    main_runner,
    walk_with_stack,
)
from baseline import is_baselined, load_baseline

_BASELINE = load_baseline("check_no_except_exception")


def _type_contains_exception(typ: ast.expr | None) -> bool:
    """Return True if ``typ`` is or contains ``Exception`` (Name/Attribute/Tuple/List).

    Recursively inspects tuple and list exception types such as
    ``except (Exception, OSError):``.

    Args:
        typ (ast.expr | None): Except handler type expression.

    Returns:
        bool: Whether Exception appears in the handler type.
    """
    if typ is None:
        return False
    if isinstance(typ, ast.Name) and typ.id == "Exception":
        return True
    if isinstance(typ, ast.Attribute) and typ.attr == "Exception":
        return True
    if isinstance(typ, (ast.Tuple, ast.List)):
        return any(_type_contains_exception(elt) for elt in typ.elts)
    return False


def check_file(path: Path, tree: ast.AST, collector: FindingCollector) -> None:
    """Flag non-allowlisted ``except Exception`` handlers in one file.

    Args:
        path (Path): Source file path.
        tree (ast.AST): Parsed AST.
        collector (FindingCollector): Finding sink.
    """
    for node, stack in walk_with_stack(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if not _type_contains_exception(node.type):
            continue
        if is_pytest_hook_in_conftest(path, stack):
            continue
        if is_baselined(path, node.lineno, _BASELINE):
            continue
        collector.report(
            path,
            node.lineno,
            "forbidden 'except Exception'; use specific exception types "
            "(allowed only in pytest_* hooks in conftest.py)",
        )


def run_check(paths: list[str], collector: FindingCollector) -> None:
    """Run the except Exception check on the given paths.

    Args:
        paths (list[str]): File paths from pre-commit (empty = whole repo).
        collector (FindingCollector): Finding sink.
    """
    for_each_parsed_file(paths, collector, check_file)


if __name__ == "__main__":
    sys.exit(main_runner(run_check))
