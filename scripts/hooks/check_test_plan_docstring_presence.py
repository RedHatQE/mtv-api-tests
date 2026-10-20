#!/usr/bin/env python3
"""Require a docstring on every pytest test class in tests/ (test_plan_schema.md)."""

from __future__ import annotations

import ast
import sys
from collections import Counter
from pathlib import Path

from _ast_utils import (
    FindingCollector,
    docstring_span,
    iter_test_classes,
    iter_test_module_files,
    main_runner,
    parse_file,
)
from baseline import is_baselined, load_baseline_for_check


def _check_file(
    path: Path,
    tree: ast.AST,
    collector: FindingCollector,
    baseline: Counter[tuple[str, str]],
) -> None:
    """Flag test classes without a docstring in one file.

    Args:
        path (Path): Source file path.
        tree (ast.AST): Parsed AST.
        collector (FindingCollector): Finding sink.
        baseline (Counter[tuple[str, str]]): Grandfathered path/fingerprint
            occurrence counts.
    """
    for node in iter_test_classes(tree):
        if docstring_span(node) is not None:
            continue
        if is_baselined(path, node.lineno, baseline, end_lineno=node.lineno):
            continue
        collector.report(
            path,
            node.lineno,
            f"test class '{node.name}' must have a class docstring with its product test plan (test_plan_schema.md)",
        )


def run_check(paths: list[str], collector: FindingCollector) -> None:
    """Run the docstring presence check on the given paths.

    Args:
        paths (list[str]): File paths from pre-commit (empty = scan tests/).
        collector (FindingCollector): Finding sink.
    """
    baseline = load_baseline_for_check("check_test_plan_docstring_presence", collector.report)
    if baseline is None:
        return

    for path in iter_test_module_files(paths):
        tree = parse_file(path, collector)
        if tree is None:
            continue
        _check_file(path, tree, collector, baseline)


if __name__ == "__main__":
    sys.exit(main_runner(run_check))
