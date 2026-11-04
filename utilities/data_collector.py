"""Data-collector artifact helpers, usable from code that has no pytest ``config``."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

_UTC_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"
_REDACTED_VALUE = "***"

_data_collector_path: Path | None = None


def configure_data_collector_path(path: Path | None) -> None:
    """Record the artifact directory of the current run for every artifact writer.

    Args:
        path (Path | None): Resolved artifact directory, or None when collection is disabled.
    """
    global _data_collector_path
    _data_collector_path = path


def data_collector_path() -> Path | None:
    """Return the artifact directory of the current run.

    Returns:
        Path | None: Artifact directory, or None when collection is disabled.
    """
    return _data_collector_path


def utc_timestamp() -> str:
    """Return the current UTC time as a compact timestamp for artifact names.

    Returns:
        str: Timestamp formatted ``%Y%m%dT%H%M%SZ``.
    """
    return datetime.now(tz=UTC).strftime(_UTC_TIMESTAMP_FORMAT)


def redact_secret(text: str, secret: str) -> str:
    """Replace every occurrence of a secret in text.

    Args:
        text (str): Text that may contain the secret.
        secret (str): Secret to replace; an empty secret leaves the text unchanged.

    Returns:
        str: Text with the secret replaced by ``***``.
    """
    if not secret:
        return text
    return text.replace(secret, _REDACTED_VALUE)
