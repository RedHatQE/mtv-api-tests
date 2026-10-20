"""Coverage subcommand — answer 'is scenario X covered?' from test-class docstrings."""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field
from pathlib import Path

import typer

from cli.mtv_api_tests.common import console

TESTS_DIR = Path(__file__).resolve().parents[2] / "tests"

PLAN_SECTIONS = ("Purpose/Regression", "Prerequisites", "Test plan", "Expected result")
_SECTION_HEADERS = {f"{section}:" for section in PLAN_SECTIONS}


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
        """Whether the docstring contains all four schema sections."""
        return set(PLAN_SECTIONS) <= set(self.sections)

    @property
    def selector(self) -> str:
        """The pytest ``-k`` selector that runs this class."""
        return f"-k {self.class_name}"


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


def _has_test_method(node: ast.ClassDef) -> bool:
    """Check whether a class defines at least one ``test_`` method.

    Args:
        node: The class definition node.

    Returns:
        True when the class has a ``test_`` method.
    """
    return any(
        isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test_")
        for item in node.body
    )


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


def parse_test_plans(tests_dir: Path) -> list[TestPlan]:
    """Parse every ``Test*`` class with a test method under the tests directory.

    Files with syntax errors are reported and skipped; classes with missing or
    non-conforming docstrings are included and flagged via ``conforming``.

    Args:
        tests_dir: Directory scanned recursively for ``test_*.py`` modules.

    Returns:
        Parsed test plans sorted by file path and class line number.
    """
    plans: list[TestPlan] = []
    repo_root = tests_dir.parent
    for path in sorted(tests_dir.rglob("test_*.py")):
        try:
            tree = ast.parse(source=path.read_text(), filename=str(path))
        except SyntaxError as exc:
            console.print(f"[yellow]Skipping {path}: syntax error: {exc}[/yellow]")
            continue
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name.startswith("Test") and _has_test_method(node):
                plans.append(_build_plan(node, path, repo_root))
    return plans


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
        console.print(f"  {plan.class_name}  [dim]{plan.file}:{plan.line}[/dim]{note}")


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
        query = " ".join(keywords)
        console.print(f"[yellow]'{query}' is not covered by any test class.[/yellow]")
        console.print(
            "[dim]Coverage is parsed from test-class docstrings; try other keywords or --missing-plans.[/dim]"
        )
        return
    console.print(f"[bold]{len(results)} test class(es) cover '{' '.join(keywords)}':[/bold]\n")
    for plan, contexts in results:
        summary = plan.summary or "[red](no docstring)[/red]"
        console.print(f"[bold]{plan.class_name}[/bold]")
        console.print(f"  Summary:  {summary}")
        console.print(f"  Markers:  {', '.join(plan.markers) or '(none)'}")
        console.print(f"  Location: {plan.file}:{plan.line}")
        console.print(f"  Run:      uv run pytest {plan.selector}")
        for context in contexts:
            console.print(f"  Matched:  {context}")
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
        typer.Exit: When no keyword and no --missing-plans is given.
    """
    words = [word for keyword in (keywords or []) for word in keyword.split()]
    if not words and not missing_plans:
        console.print("[red]Error: provide at least one keyword, or use --missing-plans[/red]")
        raise typer.Exit(code=1)

    plans = parse_test_plans(TESTS_DIR)
    if missing_plans:
        _report_missing(plans, as_json)
        return
    _report_matches(search_plans(plans, words), words, as_json)
