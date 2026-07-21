"""Baseline (grandfather) support for AGENTS.md pre-commit hooks.

Baselines live in ``scripts/hooks/baselines/<hook_id>.txt`` with one finding per
line as ``relative/posix/path:<16-char-sha256-hex>`` of the normalized source
line at the finding. Optional trailing ``#`` comments are allowed. See
``scripts/hooks/README.md``.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from pathlib import Path

_HOOKS_DIR = Path(__file__).resolve().parent
_BASELINES_DIR = _HOOKS_DIR / "baselines"
_REPO_ROOT = _HOOKS_DIR.parent.parent

# Resolved path -> split source lines; populated once per path per process.
_LINE_CACHE: dict[Path, list[str]] = {}

# path:16-hex, optional whitespace and # comment
_ENTRY_RE = re.compile(r"^(?P<path>[^:#\s][^:#]*):(?P<fp>[0-9a-f]{16})(?:\s*(?:#.*)?)?$")


def _normalize_line(line: str) -> str:
    """Normalize a source line for fingerprinting.

    Strips only trailing ``\\n`` / ``\\r``. Leading and internal whitespace are
    preserved so edits inside string literals (and indentation) change the
    fingerprint.

    Args:
        line (str): Raw source line (with or without trailing newline).

    Returns:
        str: Line content without trailing CR/LF.
    """
    return line.rstrip("\r\n")


def _content_fingerprint(line: str) -> str:
    """Return the 16-char SHA-256 hex fingerprint of a normalized source line.

    Args:
        line (str): Raw source line.

    Returns:
        str: First 16 hex characters of ``sha256(_normalize_line(line))``.
    """
    normalized = _normalize_line(line)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def load_baseline(
    hook_id: str,
) -> tuple[set[tuple[str, str]], list[tuple[Path, int, str]]]:
    """Load grandfathered findings for ``hook_id``.

    Never raises on malformed lines. Callers should report ``errors`` via
    ``FindingCollector`` and abort the check (exit 1) when the list is
    non-empty.

    Args:
        hook_id (str): Hook identifier (baseline filename stem, e.g.
            ``check_no_except_exception``).

    Returns:
        tuple[set[tuple[str, str]], list[tuple[Path, int, str]]]:
        ``(entries, errors)`` where ``entries`` is the set of
        ``(repo-relative posix path, fingerprint)`` pairs (empty if the
        baseline file does not exist), and ``errors`` is a list of
        ``(baseline_path, 1-based lineno, message)`` for each malformed
        non-comment line. Valid lines before/after malformed ones are still
        included in ``entries``.
    """
    path = _BASELINES_DIR / f"{hook_id}.txt"
    if not path.is_file():
        return set(), []

    entries: set[tuple[str, str]] = set()
    errors: list[tuple[Path, int, str]] = []
    for lineno, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _ENTRY_RE.match(line)
        if match is None:
            errors.append((path, lineno, f"malformed baseline entry: {raw_line!r}"))
            continue
        entries.add((match.group("path"), match.group("fp")))
    return entries, errors


def load_baseline_for_check(
    hook_id: str,
    report: Callable[[Path, int, str], None],
) -> set[tuple[str, str]] | None:
    """Load a baseline for a hook run, reporting malformed lines via ``report``.

    Args:
        hook_id (str): Hook identifier (baseline filename stem).
        report (Callable[[Path, int, str], None]): Callback invoked as
            ``report(path, lineno, msg)`` for each malformed baseline line
            (typically ``FindingCollector.report``).

    Returns:
        set[tuple[str, str]] | None: Baseline entries when the file is clean
        (or missing). ``None`` when any malformed entries were reported; the
        caller should abort the rest of the check so the hook exits 1 without
        a traceback.
    """
    entries, errors = load_baseline(hook_id)
    for path, lineno, msg in errors:
        report(path, lineno, msg)
    if errors:
        return None
    return entries


def repo_relative_posix(path: Path) -> str:
    """Return ``path`` as a repo-root-relative POSIX string.

    Args:
        path (Path): Absolute or cwd-relative source path.

    Returns:
        str: Path relative to the repository root using ``/`` separators.
    """
    return path.resolve().relative_to(_REPO_ROOT).as_posix()


def is_baselined(
    path: Path,
    lineno: int,
    baseline: set[tuple[str, str]],
) -> bool:
    """Return True if the source line at ``path:lineno`` matches a baseline entry.

    Reads the current file line (cached per resolved path for the process),
    fingerprints its normalized content, and checks
    ``(repo_relative_posix(path), fingerprint)`` against ``baseline``. Matching
    ignores lineno, so line drift from refactors still suppresses the same
    content. Content changes are not suppressed. Missing or unreadable lines
    return False (do not suppress).

    Args:
        path (Path): Source file path for the finding.
        lineno (int): 1-based line number of the finding.
        baseline (set[tuple[str, str]]): Loaded baseline entries.

    Returns:
        bool: Whether this finding is grandfathered.
    """
    try:
        resolved = path.resolve()
        lines = _LINE_CACHE.get(resolved)
        if lines is None:
            lines = resolved.read_text(encoding="utf-8").splitlines()
            _LINE_CACHE[resolved] = lines
    except (OSError, UnicodeError):
        return False
    if lineno < 1 or lineno > len(lines):
        return False
    fingerprint = _content_fingerprint(lines[lineno - 1])
    return (repo_relative_posix(path), fingerprint) in baseline
