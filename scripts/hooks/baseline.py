"""Baseline (grandfather) support for AGENTS.md pre-commit hooks.

Baselines live in ``scripts/hooks/baselines/<hook_id>.txt`` with one finding per
line as ``relative/posix/path:lineno``. Comments (``#``) and blank lines are
ignored. See ``scripts/hooks/README.md``.
"""

from __future__ import annotations

from pathlib import Path

_HOOKS_DIR = Path(__file__).resolve().parent
_BASELINES_DIR = _HOOKS_DIR / "baselines"
_REPO_ROOT = _HOOKS_DIR.parent.parent


def load_baseline(hook_id: str) -> set[tuple[str, int]]:
    """Load grandfathered findings for ``hook_id``.

    Args:
        hook_id (str): Hook identifier (baseline filename stem, e.g.
            ``check_no_except_exception``).

    Returns:
        set[tuple[str, int]]: Set of ``(repo-relative posix path, lineno)``
        pairs. Empty if the baseline file does not exist.

    Raises:
        ValueError: If a non-comment baseline line is malformed.
    """
    path = _BASELINES_DIR / f"{hook_id}.txt"
    if not path.is_file():
        return set()

    entries: set[tuple[str, int]] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"Malformed baseline entry in {path}: {raw_line!r}")
        rel_path, lineno_str = line.rsplit(":", 1)
        if not rel_path or not lineno_str.isdigit():
            raise ValueError(f"Malformed baseline entry in {path}: {raw_line!r}")
        entries.add((rel_path, int(lineno_str)))
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
    baseline: set[tuple[str, int]],
) -> bool:
    """Return True if ``path:lineno`` is listed in ``baseline``.

    Args:
        path (Path): Source file path for the finding.
        lineno (int): 1-based line number.
        baseline (set[tuple[str, int]]): Loaded baseline entries.

    Returns:
        bool: Whether this finding is grandfathered.
    """
    return (repo_relative_posix(path), lineno) in baseline
