"""Secure shared directories for pytest-xdist file locks."""

from __future__ import annotations

import os
from contextlib import ExitStack
from pathlib import Path


def ensure_secure_shared_lock_dir(lock_dir: Path) -> None:
    """Validate permissions on a cross-worker shared lock directory.

    Opens the directory with ``O_NOFOLLOW`` and validates ownership via ``fstat`` on
    the resulting file descriptor, then sets mode with ``fchmod`` on that fd. This
    avoids acting on a path that was swapped to a symlink after ``mkdir``. A small
    mkdir-to-open window remains; that is acceptable for this pytest-xdist lock path
    under the user's temp directory in controlled CI.

    Args:
        lock_dir (Path): Directory used for pytest-xdist file locks.

    Returns:
        None

    Raises:
        PermissionError: If the directory is a symlink or owned by another user.
    """
    lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    try:
        dir_fd = os.open(str(lock_dir), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as err:
        raise PermissionError(
            f"Security error: cannot open shared directory {lock_dir} without following symlinks "
            f"({err}). This may indicate a hijack attempt."
        ) from err

    with ExitStack() as stack:
        stack.callback(os.close, dir_fd)
        dir_stat = os.fstat(dir_fd)
        current_uid = os.getuid()
        if dir_stat.st_uid != current_uid:
            raise PermissionError(
                f"Security error: shared directory {lock_dir} is owned by uid {dir_stat.st_uid}, "
                f"expected current user uid {current_uid}. This may indicate a hijack attempt."
            )
        os.fchmod(dir_fd, 0o700)
