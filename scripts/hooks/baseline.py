"""Baseline (grandfather) support for AGENTS.md pre-commit hooks.

Baselines live in ``scripts/hooks/baselines/<hook_id>.txt`` with one finding per
line as ``relative/posix/path:<16-char-sha256-hex>`` of the normalized source
span at the finding. Optional trailing ``#`` comments are allowed. Duplicate
identical rows are allowed and count as separate occurrences. See
``scripts/hooks/README.md``.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Callable
from pathlib import Path

_HOOKS_DIR = Path(__file__).resolve().parent
_BASELINES_DIR = _HOOKS_DIR / "baselines"
_REPO_ROOT = _HOOKS_DIR.parent.parent

# Resolved path -> split source lines; populated once per path per process.
_LINE_CACHE: dict[Path, list[str]] = {}

# path:16-hex, optional whitespace and # comment
_ENTRY_RE = re.compile(r"^(?P<path>[^:#\s][^:#]*):(?P<fp>[0-9a-f]{16})(?:\s*(?:#.*)?)?$")


def _baseline_path_error(rel_path: str) -> str | None:
    """Return an error message if ``rel_path`` is not a valid baseline path key.

    Baseline keys must be repo-root-relative POSIX paths (no absolute paths,
    backslashes, ``.`` / ``..`` components, or ``./`` prefix) so they can match
    keys produced by :func:`is_baselined`.

    Args:
        rel_path (str): Path portion of a baseline entry.

    Returns:
        str | None: Human-readable error, or ``None`` when the path is valid.
    """
    if not rel_path:
        return "baseline path is empty"
    if "\\" in rel_path:
        return f"baseline path must use POSIX separators (/), got {rel_path!r}"
    if rel_path.startswith("/") or (len(rel_path) >= 2 and rel_path[1] == ":"):
        return f"baseline path must be repo-relative, not absolute: {rel_path!r}"
    if rel_path.startswith("./"):
        return f"baseline path must not start with './': {rel_path!r}"
    if any(part in (".", "..") for part in rel_path.split("/")):
        return f"baseline path must not contain '.' or '..' components: {rel_path!r}"
    return None


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


def _fingerprint_text(text: str) -> str:
    """Return the 16-char SHA-256 hex fingerprint of ``text``.

    Args:
        text (str): Already-normalized text (single line or joined span).

    Returns:
        str: First 16 hex characters of ``sha256(text)``.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _span_fingerprint(lines: list[str], lineno: int, end_lineno: int | None = None) -> str | None:
    """Fingerprint a source span for baseline matching.

    Each line is normalized with :func:`_normalize_line` (strip trailing CR/LF
    only). When ``end_lineno`` is set and greater than ``lineno``, normalized
    lines ``lines[lineno-1:end_lineno]`` are joined with ``\\n`` and hashed.
    Otherwise only the single line at ``lineno`` is hashed (same as the
    historical single-line scheme).

    Args:
        lines (list[str]): File lines from ``str.splitlines()`` (no newlines).
        lineno (int): 1-based start line.
        end_lineno (int | None): Optional 1-based inclusive end line from the
            AST node (``node.end_lineno``).

    Returns:
        str | None: 16-char hex fingerprint, or ``None`` if the span is out of
        range.
    """
    if lineno < 1 or lineno > len(lines):
        return None
    end = end_lineno if end_lineno is not None and end_lineno > lineno else lineno
    if end > len(lines):
        return None
    span = "\n".join(_normalize_line(line) for line in lines[lineno - 1 : end])
    return _fingerprint_text(span)


def _cached_lines(resolved: Path) -> list[str] | None:
    """Return cached source lines for ``resolved``, or ``None`` on I/O error.

    Args:
        resolved (Path): Absolute path to the source file.

    Returns:
        list[str] | None: Lines from ``splitlines()``, or ``None`` if unreadable.
    """
    try:
        lines = _LINE_CACHE.get(resolved)
        if lines is None:
            lines = resolved.read_text(encoding="utf-8").splitlines()
            _LINE_CACHE[resolved] = lines
        return lines
    except (OSError, UnicodeError):
        return None


def load_baseline(
    hook_id: str,
) -> tuple[Counter[tuple[str, str]], list[tuple[Path, int, str]]]:
    """Load grandfathered findings for ``hook_id``.

    Never raises on malformed lines or unreadable baseline files. Callers
    should report ``errors`` via ``FindingCollector`` and abort the check
    (exit 1) when the list is non-empty.

    Duplicate identical ``path:fingerprint`` rows increment the occurrence
    count so each grandfathered finding consumes one occurrence at match time.

    Args:
        hook_id (str): Hook identifier (baseline filename stem, e.g.
            ``check_no_except_exception``).

    Returns:
        tuple[Counter[tuple[str, str]], list[tuple[Path, int, str]]]:
        ``(entries, errors)`` where ``entries`` maps
        ``(repo-relative posix path, fingerprint)`` to occurrence counts
        (empty if the baseline file does not exist or cannot be read), and
        ``errors`` is a list of ``(baseline_path, 1-based lineno, message)``
        for each malformed non-comment line or a single read-failure finding
        at lineno 1. Valid lines before/after malformed ones are still
        included in ``entries``.
    """
    path = _BASELINES_DIR / f"{hook_id}.txt"
    if not path.is_file():
        return Counter(), []

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as err:
        return Counter(), [(path, 1, f"failed to read baseline: {err}")]

    entries: Counter[tuple[str, str]] = Counter()
    errors: list[tuple[Path, int, str]] = []
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _ENTRY_RE.match(line)
        if match is None:
            errors.append((path, lineno, f"malformed baseline entry: {raw_line!r}"))
            continue
        rel_path = match.group("path")
        if path_err := _baseline_path_error(rel_path):
            errors.append((path, lineno, path_err))
            continue
        entries[(rel_path, match.group("fp"))] += 1
    return entries, errors


def load_baseline_for_check(
    hook_id: str,
    report: Callable[[Path, int, str], None],
) -> Counter[tuple[str, str]] | None:
    """Load a baseline for a hook run, reporting malformed lines via ``report``.

    Args:
        hook_id (str): Hook identifier (baseline filename stem).
        report (Callable[[Path, int, str], None]): Callback invoked as
            ``report(path, lineno, msg)`` for each malformed baseline line
            (typically ``FindingCollector.report``).

    Returns:
        Counter[tuple[str, str]] | None: Baseline occurrence counts when the
        file is clean (or missing). ``None`` when any malformed entries were
        reported; the caller should abort the rest of the check so the hook
        exits 1 without a traceback.
    """
    entries, errors = load_baseline(hook_id)
    for path, lineno, msg in errors:
        report(path, lineno, msg)
    if errors:
        return None
    return entries


def repo_relative_posix(path: Path) -> str:
    """Return ``path`` as a repo-root-relative POSIX string.

    Paths outside the repository root do not raise: ``relative_to`` raises
    ``ValueError`` in that case, and this function returns
    ``path.resolve().as_posix()`` instead.

    Args:
        path (Path): Absolute or cwd-relative source path.

    Returns:
        str: Path relative to the repository root using ``/`` separators, or
        the resolved absolute POSIX path when ``path`` is outside the repo.
    """
    resolved = path.resolve()
    try:
        return resolved.relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        return resolved.as_posix()


def is_baselined(
    path: Path,
    lineno: int,
    baseline: Counter[tuple[str, str]],
    end_lineno: int | None = None,
) -> bool:
    """Return True if the source span at ``path:lineno`` consumes a baseline occurrence.

    Reads the current file (cached per resolved path for the process),
    fingerprints the normalized span (see :func:`_span_fingerprint`), and
    looks up ``(repo-relative path, fingerprint)`` in ``baseline``. When a
    count is available (``> 0``), decrements it by one and returns True.
    Returns False when the count is exhausted or the key is absent. Matching
    ignores lineno, so line drift from refactors still suppresses the same
    content. Content changes are not suppressed. Missing or unreadable spans,
    and paths outside the repository, return False (do not suppress).

    When ``end_lineno`` is provided and greater than ``lineno``, the fingerprint
    covers the full AST node span (e.g. multi-line ``raise``, ``import``,
    ``except`` handler body, or ``class`` body). Callers should pass
    ``node.end_lineno`` for nodes that report findings.

    Args:
        path (Path): Source file path for the finding.
        lineno (int): 1-based start line number of the finding.
        baseline (Counter[tuple[str, str]]): Loaded baseline occurrence counts
            (mutated in place when a count is consumed).
        end_lineno (int | None): Optional 1-based inclusive end line
            (``node.end_lineno``). When omitted or ``<= lineno``, only the
            single start line is fingerprinted.

    Returns:
        bool: Whether this finding is grandfathered (one occurrence consumed).
    """
    try:
        resolved = path.resolve()
        rel_posix = resolved.relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        return False
    lines = _cached_lines(resolved)
    if lines is None:
        return False
    fingerprint = _span_fingerprint(lines, lineno, end_lineno)
    if fingerprint is None:
        return False
    key = (rel_posix, fingerprint)
    if baseline[key] <= 0:
        return False
    baseline[key] -= 1
    return True
