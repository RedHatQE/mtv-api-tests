#!/usr/bin/env python3
"""Require test-class docstrings to follow the test_plan_schema.md structure."""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

from _ast_utils import (
    FindingCollector,
    docstring_span,
    iter_test_classes,
    iter_test_module_files,
    main_runner,
    parse_file,
)

_SECTION_NAMES: tuple[str, ...] = (
    "Purpose/Regression",
    "Prerequisites",
    "Test plan",
    "Expected result",
)
_SECTION_RE = re.compile(r"^\s*(?:" + "|".join(re.escape(name) for name in _SECTION_NAMES) + r"):\s*$")
_NUMBERED_STEP_RE = re.compile(r"^(\s*)(\d+)\.\s+(\S.*)$")
_HEADING_RE = re.compile(r"^\s*[A-Za-z][\w /-]*:\s*$")


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


def _validate_test_plan(lines: list[str]) -> list[str]:
    """Validate the numbered-step structure of a Test plan section.

    The first nonblank line must be a numbered step (``N. text``); its
    indentation defines the step level. Later nonblank lines must be
    numbered steps at that level or continuations indented deeper.
    Unnumbered standalone actions, malformed markers (``2.Do``), and empty
    markers (``1.`` with no text) are schema violations.

    Args:
        lines (list[str]): Content lines of the Test plan section.

    Returns:
        list[str]: Human-readable problems; empty when the section conforms.
    """
    problems: list[str] = []
    step_indent: int | None = None
    numbers: list[int] = []
    for line in lines:
        if not line.strip():
            continue
        line_indent = len(line) - len(line.lstrip())
        if step_indent is not None and line_indent > step_indent:
            continue
        match = _NUMBERED_STEP_RE.match(line)
        if match is None:
            problems.append(f"test plan line is not a numbered step or a continuation: {line.strip()!r}")
            continue
        indent, number = match.group(1), int(match.group(2))
        if step_indent is None:
            step_indent = len(indent)
        elif len(indent) != step_indent:
            problems.append(f"test plan step {number} is not at the step indentation level: {line.strip()!r}")
            continue
        numbers.append(number)
    if not numbers:
        problems.append("section 'Test plan' has no numbered steps")
    elif numbers != list(range(1, len(numbers) + 1)):
        problems.append("section 'Test plan' steps are not consecutively numbered from 1")
    return problems


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
    elif first_nonempty in header_positions.values() or _HEADING_RE.match(lines[first_nonempty]):
        problems.append("missing one-sentence scenario summary before the first section")

    for name, (start, end) in _section_content_range(lines, header_positions).items():
        content = [line for line in lines[start:end] if line.strip() and not _HEADING_RE.match(line)]
        if not content:
            problems.append(f"section '{name}' has no content")
            continue
        if name != "Test plan":
            continue
        problems.extend(_validate_test_plan(lines[start:end]))

    return problems


def _check_file(path: Path, tree: ast.Module, collector: FindingCollector) -> None:
    """Flag test classes with a missing or non-conforming plan docstring.

    There is no baseline and no suppression: the check always reflects the
    current state of the files.

    Args:
        path (Path): Source file path.
        tree (ast.AST): Parsed AST.
        collector (FindingCollector): Finding sink.
    """
    for node in iter_test_classes(tree):
        span = docstring_span(node)
        if span is None:
            collector.report(
                path,
                node.lineno,
                f"test class '{node.name}' must have a class docstring with its "
                "product test plan (test_plan_schema.md)",
            )
            continue
        text, lineno, _ = span
        problems = _validate_docstring(text)
        if problems:
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
    for path in iter_test_module_files(paths):
        tree = parse_file(path, collector)
        if tree is None:
            continue
        _check_file(path, tree, collector)


if __name__ == "__main__":
    sys.exit(main_runner(run_check))
