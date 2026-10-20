"""Coverage subcommand — answer 'is scenario X covered?' from test-class docstrings."""

from __future__ import annotations

import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import typer
from rich.markup import escape

from cli.mtv_api_tests.common import console

TESTS_DIR = Path(__file__).resolve().parents[2] / "tests"

PLAN_SECTIONS = ("Purpose/Regression", "Prerequisites", "Test plan", "Expected result")
_SECTION_HEADERS = {f"{section}:" for section in PLAN_SECTIONS}
_NUMBERED_STEP = re.compile(r"^\s*(\d+)\.\s")


@dataclass
class TestPlan:
    """A test class parsed from source, with its plan docstring and markers.

    Attributes:
        class_name: Name of the ``Test*`` class.
        file: Path of the defining module, relative to the repository root.
        line: Line number of the class definition.
        summary: First non-empty docstring line (one-sentence scenario summary).
        docstring: Raw class docstring (empty string when missing).
        sections: Parsed plan sections keyed by section header.
        markers: pytest markers applied at class level.
    """

    class_name: str
    file: str
    line: int
    summary: str
    docstring: str = ""
    sections: dict[str, str] = field(default_factory=dict)
    markers: list[str] = field(default_factory=list)

    @property
    def conforming(self) -> bool:
        """Whether the docstring follows the full test-plan schema.

        Requires a non-empty summary, all four sections in schema order with
        non-empty bodies, and strictly consecutive numbered steps in the plan.
        """
        if not self.summary:
            return False
        if list(self.sections) != list(PLAN_SECTIONS):
            return False
        if any(not body.strip() for body in self.sections.values()):
            return False
        steps = [
            int(match.group(1))
            for line in self.sections["Test plan"].splitlines()
            if (match := _NUMBERED_STEP.match(line))
        ]
        return bool(steps) and steps == list(range(1, len(steps) + 1))

    @property
    def selector(self) -> str:
        """The pytest node ID (``file::ClassName``) that runs exactly this class."""
        return f"{self.file}::{self.class_name}"


def _parse_docstring(docstring: str) -> tuple[str, dict[str, str]]:
    """Split a class docstring into a one-sentence summary and plan sections.

    Args:
        docstring: Raw class docstring (may be empty).

    Returns:
        Tuple of (summary, sections) where summary is the first non-empty line
        and sections maps each recognized section header to its text.
    """
    summary = ""
    sections: dict[str, str] = {}
    header = ""
    body: list[str] = []

    def _flush() -> None:
        """Store the accumulated body lines under the current section header."""
        if header:
            sections[header] = "\n".join(body).strip()

    for line in docstring.splitlines():
        name = line.strip()
        if name in _SECTION_HEADERS:
            _flush()
            header, body = name[:-1], []
        elif header:
            body.append(line)
        elif name and not summary:
            summary = name
    _flush()
    return summary, sections


def _mark_name(decorator: ast.expr) -> str | None:
    """Return the marker name of a ``pytest.mark.<name>`` decorator, if any.

    Args:
        decorator: A decorator AST node (bare or called).

    Returns:
        The marker name, or None when the decorator is not a pytest mark.
    """
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    if (
        isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Attribute)
        and target.value.attr == "mark"
        and isinstance(target.value.value, ast.Name)
        and target.value.value.id == "pytest"
    ):
        return target.attr
    return None


def _has_test_method(node: ast.ClassDef, classes: dict[str, ast.ClassDef]) -> bool:
    """Check whether a class declares or inherits at least one ``test_`` method.

    Inheritance is resolved through same-module base classes. A base that
    cannot be resolved (defined in another module) is conservatively treated
    as test-bearing, so runnable classes are never dropped from coverage.

    Args:
        node: The class definition node.
        classes: Class definitions of the same module, by name.

    Returns:
        True when the class has (or may have) a ``test_`` method.
    """
    if any(
        isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test_")
        for item in node.body
    ):
        return True
    for base in node.bases:
        if isinstance(base, ast.Name):
            base_node = classes.get(base.id)
            if base_node is None or _has_test_method(base_node, classes):
                return True
    return False


def _build_plan(node: ast.ClassDef, path: Path, repo_root: Path) -> TestPlan:
    """Build a TestPlan from a class definition node.

    Args:
        node: The ``Test*`` class definition node.
        path: Path of the module defining the class.
        repo_root: Repository root used to make the path relative.

    Returns:
        The parsed TestPlan.
    """
    docstring = ast.get_docstring(node) or ""
    summary, sections = _parse_docstring(docstring)
    markers = [name for decorator in node.decorator_list if (name := _mark_name(decorator)) and name != "parametrize"]
    return TestPlan(
        class_name=node.name,
        file=str(path.relative_to(repo_root)),
        line=node.lineno,
        summary=summary,
        docstring=docstring,
        sections=sections,
        markers=markers,
    )


def parse_test_plans(tests_dir: Path) -> tuple[list[TestPlan], list[str]]:
    """Parse every ``Test*`` class with a test method under the tests directory.

    Classes with missing or non-conforming docstrings are included and flagged
    via ``conforming``. Modules that cannot be parsed do not vanish silently:
    they are reported as errors so callers can fail closed.

    Args:
        tests_dir: Directory scanned recursively for ``test_*.py`` modules.

    Returns:
        Tuple of (plans, errors): parsed plans sorted by file path and class
        line number, and human-readable errors for unparsable modules.
    """
    plans: list[TestPlan] = []
    errors: list[str] = []
    repo_root = tests_dir.parent
    for path in sorted(tests_dir.rglob("test_*.py")):
        try:
            tree = ast.parse(source=path.read_text(), filename=str(path))
        except SyntaxError as exc:
            errors.append(f"{path}: syntax error: {exc}")
            continue
        classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name.startswith("Test") and _has_test_method(node, classes):
                plans.append(_build_plan(node, path, repo_root))
    return plans, errors


def _normalize(text: str) -> str:
    """Normalize text for case-insensitive matching.

    Underscores become spaces so 'shared disk' matches the ``shared_disk`` marker
    and CamelCase class names read naturally.

    Args:
        text: Text to normalize.

    Returns:
        Lowercased text with underscores replaced by spaces.
    """
    return text.lower().replace("_", " ")


def _match_context(plan: TestPlan, keyword: str) -> str:
    """Return trimmed evidence of where a keyword matched a plan.

    Args:
        plan: The matched test plan.
        keyword: The matched keyword.

    Returns:
        The trimmed docstring line containing the keyword, or a class-name/marker note.
    """
    keyword = _normalize(keyword)
    for line in plan.docstring.splitlines():
        if keyword in _normalize(line):
            return " ".join(line.split())
    if keyword in _normalize(plan.class_name):
        return f"class name: {plan.class_name}"
    for marker in plan.markers:
        if keyword in _normalize(marker):
            return f"marker: {marker}"
    return keyword


def search_plans(plans: list[TestPlan], keywords: list[str]) -> list[tuple[TestPlan, list[str]]]:
    """Find test plans matching every keyword (AND semantics, case-insensitive).

    Args:
        plans: Parsed test plans to search.
        keywords: Keywords searched across docstring, class name, and markers.

    Returns:
        Tuples of (plan, deduplicated match context lines).
    """
    results: list[tuple[TestPlan, list[str]]] = []
    lowered = [_normalize(keyword) for keyword in keywords]
    for plan in plans:
        haystack = _normalize(f"{plan.class_name} {' '.join(plan.markers)} {plan.docstring}")
        if all(keyword in haystack for keyword in lowered):
            contexts: list[str] = []
            for keyword in lowered:
                context = _match_context(plan, keyword)
                if context not in contexts:
                    contexts.append(context)
            results.append((plan, contexts))
    return results


def _plan_json(plan: TestPlan, contexts: list[str]) -> dict[str, object]:
    """Build the JSON-serializable representation of a matched plan.

    Args:
        plan: The matched test plan.
        contexts: Matched keyword context lines.

    Returns:
        Dict with class, summary, markers, location, selector, and match contexts.
    """
    return {
        "class": plan.class_name,
        "summary": plan.summary,
        "markers": plan.markers,
        "file": plan.file,
        "line": plan.line,
        "selector": plan.selector,
        "conforming": plan.conforming,
        "matches": contexts,
    }


def _report_missing(plans: list[TestPlan], as_json: bool) -> None:
    """List test classes whose docstring is missing or non-conforming.

    Args:
        plans: Parsed test plans.
        as_json: When True, print JSON instead of human-readable output.
    """
    missing = [plan for plan in plans if not plan.conforming]
    if as_json:
        entries = [
            {
                "class": plan.class_name,
                "file": plan.file,
                "line": plan.line,
                "summary": plan.summary,
                "docstring": bool(plan.docstring),
            }
            for plan in missing
        ]
        print(json.dumps({"missing_plans": entries}, indent=2))
        return
    console.print(f"[bold]Classes with missing or non-conforming test plans ({len(missing)}):[/bold]\n")
    for plan in missing:
        note = "" if plan.docstring else "  [red](no docstring)[/red]"
        console.print(f"  {escape(plan.class_name)}  [dim]{escape(plan.file)}:{plan.line}[/dim]{note}")


def _report_matches(results: list[tuple[TestPlan, list[str]]], keywords: list[str], as_json: bool) -> None:
    """Report search results for human or JSON consumption.

    Args:
        results: Matched plans with their context lines.
        keywords: The searched keywords.
        as_json: When True, print JSON instead of human-readable output.
    """
    if as_json:
        payload = {
            "query": keywords,
            "matches": [_plan_json(plan, contexts) for plan, contexts in results],
        }
        print(json.dumps(payload, indent=2))
        return
    if not results:
        query = escape(" ".join(keywords))
        console.print(f"[yellow]'{query}' is not covered by any test class.[/yellow]")
        console.print(
            "[dim]Coverage is parsed from test-class docstrings; try other keywords or --missing-plans.[/dim]"
        )
        return
    console.print(f"[bold]{len(results)} test class(es) cover '{escape(' '.join(keywords))}':[/bold]\n")
    for plan, contexts in results:
        summary = escape(plan.summary) or "[red](no docstring)[/red]"
        console.print(f"[bold]{escape(plan.class_name)}[/bold]")
        console.print(f"  Summary:  {summary}")
        console.print(f"  Markers:  {', '.join(escape(marker) for marker in plan.markers) or '(none)'}")
        console.print(f"  Location: {escape(plan.file)}:{plan.line}")
        console.print(f"  Run:      uv run pytest {plan.selector}")
        for context in contexts:
            console.print(f"  Matched:  {escape(context)}")
        if not plan.conforming:
            console.print("  [dim](docstring does not follow the test-plan schema yet)[/dim]")
        console.print("")


def coverage_command(keywords: list[str], missing_plans: bool, as_json: bool) -> None:
    """Answer 'is scenario X covered?' by searching test-class plan docstrings.

    Docstrings are parsed on demand (AST, no test-module imports) and are the
    single source of truth; no index is stored. No matches exits 0 with a
    message, which is friendlier for scripting than a non-zero exit.

    Args:
        keywords: Keywords to search (case-insensitive, AND semantics;
            quoted phrases are split into words).
        missing_plans: When True, list classes with missing/non-conforming plans.
        as_json: When True, output JSON for scripting.

    Raises:
        typer.Exit: When no keyword and no --missing-plans is given, when the
            test sources are not available, or when a test module cannot be
            parsed (fail closed; diagnostics go to stderr so stdout stays
            parseable in --json mode).
    """
    words = [word for keyword in keywords for word in keyword.split()]
    if not words and not missing_plans:
        print("Error: provide at least one keyword, or use --missing-plans", file=sys.stderr)
        raise typer.Exit(code=1)
    if not TESTS_DIR.is_dir():
        print(
            f"Error: test sources not found at {TESTS_DIR}; the coverage command requires a repository checkout.",
            file=sys.stderr,
        )
        raise typer.Exit(code=1)
    plans, errors = parse_test_plans(TESTS_DIR)
    if errors:
        for error in errors:
            print(f"Error: unparsable test module: {error}", file=sys.stderr)
        raise typer.Exit(code=1)
    if missing_plans:
        _report_missing(plans, as_json)
        return
    _report_matches(search_plans(plans, words), words, as_json)
