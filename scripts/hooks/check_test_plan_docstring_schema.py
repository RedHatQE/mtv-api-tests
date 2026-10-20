#!/usr/bin/env python3
"""Require test-class docstrings to follow the test_plan_schema.md structure."""

from __future__ import annotations

import ast
import re
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

_SECTION_NAMES: tuple[str, ...] = (
    "Purpose/Regression",
    "Prerequisites",
    "Test plan",
    "Expected result",
)
_SECTION_RE = re.compile(r"^\s*(?:" + "|".join(re.escape(name) for name in _SECTION_NAMES) + r"):\s*$")
_NUMBERED_STEP_RE = re.compile(r"^\s*(\d+)\.\s")


def _section_content_range(lines: list[str], header_positions: dict[str, int]) -> dict[str, tuple[int, int]]:
    """Return the (exclusive) content line range per present section.

    Args:
        lines (list[str]): Docstring lines.
        header_positions (dict[str, int]): Section name to header line index.

    Returns:
        dict[str, tuple[int, int]]: Section name to ``(start, end)`` line
        index range (content only, excluding the header and the next header).
    """
    ordered = sorted(header_positions.items(), key=lambda item: item[1])
    ranges: dict[str, tuple[int, int]] = {}
    for idx, (name, position) in enumerate(ordered):
        end = ordered[idx + 1][1] if idx + 1 < len(ordered) else len(lines)
        ranges[name] = (position + 1, end)
    return ranges


def _validate_docstring(docstring: str) -> list[str]:
    """Validate a test-plan docstring against the schema structure.

    Args:
        docstring (str): Raw class docstring content.

    Returns:
        list[str]: Human-readable problems; empty when the docstring conforms.
    """
    lines = docstring.splitlines()
    problems: list[str] = []

    header_positions: dict[str, int] = {}
    for idx, line in enumerate(lines):
        match = _SECTION_RE.match(line)
        if match is None:
            continue
        name = match.group(0).strip().rstrip(":")
        if name in header_positions:
            problems.append(f"duplicate section '{name}'")
        else:
            header_positions[name] = idx

    missing = [name for name in _SECTION_NAMES if name not in header_positions]
    if missing:
        problems.append("missing section(s): " + ", ".join(missing))

    present = [name for name in _SECTION_NAMES if name in header_positions]
    positions = [header_positions[name] for name in present]
    if positions != sorted(positions):
        problems.append("sections out of schema order (expected: " + ", ".join(_SECTION_NAMES) + ")")

    first_nonempty = next((idx for idx, line in enumerate(lines) if line.strip()), None)
    if first_nonempty is None:
        problems.append("docstring is empty")
    elif first_nonempty in header_positions.values():
        problems.append("missing one-sentence scenario summary before the first section")

    for name, (start, end) in _section_content_range(lines, header_positions).items():
        content = [line for line in lines[start:end] if line.strip()]
        if not content:
            problems.append(f"section '{name}' has no content")
            continue
        if name != "Test plan":
            continue
        steps = [int(match.group(1)) for match in (_NUMBERED_STEP_RE.match(line) for line in lines[start:end]) if match]
        if not steps:
            problems.append("section 'Test plan' has no numbered steps")
        elif steps != list(range(1, len(steps) + 1)):
            problems.append("section 'Test plan' steps are not consecutively numbered from 1")

    return problems


def _check_file(
    path: Path,
    tree: ast.AST,
    collector: FindingCollector,
    baseline: Counter[tuple[str, str]],
) -> None:
    """Flag test classes whose docstring does not follow the schema.

    Docless classes are skipped here: docstring presence is owned by the
    check_test_plan_docstring_presence hook.

    Args:
        path (Path): Source file path.
        tree (ast.AST): Parsed AST.
        collector (FindingCollector): Finding sink.
        baseline (Counter[tuple[str, str]]): Grandfathered path/fingerprint
            occurrence counts.
    """
    for node in iter_test_classes(tree):
        span = docstring_span(node)
        if span is None:
            continue
        text, lineno, end_lineno = span
        problems = _validate_docstring(text)
        if not problems:
            continue
        if is_baselined(path, lineno, baseline, end_lineno=end_lineno):
            continue
        collector.report(
            path,
            lineno,
            f"test class '{node.name}' test plan does not follow test_plan_schema.md: " + "; ".join(problems),
        )


def run_check(paths: list[str], collector: FindingCollector) -> None:
    """Run the test-plan schema check on the given paths.

    Args:
        paths (list[str]): File paths from pre-commit (empty = scan tests/).
        collector (FindingCollector): Finding sink.
    """
    baseline = load_baseline_for_check("check_test_plan_docstring_schema", collector.report)
    if baseline is None:
        return

    for path in iter_test_module_files(paths):
        tree = parse_file(path, collector)
        if tree is None:
            continue
        _check_file(path, tree, collector, baseline)


if __name__ == "__main__":
    sys.exit(main_runner(run_check))
