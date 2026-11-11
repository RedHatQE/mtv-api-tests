"""Turn SIGINT/SIGTERM into pytest's interrupted shutdown, so the tracked teardown still runs.

Issue #842: teardown only ran on the clean session-finish path, so an interrupted run left its
clones and other resources behind on the lab. A handler installed at session start records the
signal and interrupts pytest, which unwinds through the normal session-finish path where the
tracked teardown lives. Ending the process from the handler instead would skip that teardown
entirely, and on an xdist controller it would kill the workers before they run theirs.
"""

from __future__ import annotations

import signal
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from queue import Empty
from types import FrameType

import pytest
from simple_logger.logger import get_logger
from xdist.dsession import DSession

LOGGER = get_logger(__name__)

# signal.signal returns the handler it replaced: a callable, SIG_DFL/SIG_IGN, or None.
_SignalHandler = Callable[[int, FrameType | None], object] | int | None

# Upper bound on the controller's wait for its workers to finish their own teardown. The
# controller's own session finish kills workers that outlive a short exit timeout, so waiting here
# is what keeps a worker deleting resources from dying mid-cleanup; the bound keeps a stuck worker
# from hanging the interrupted run forever.
_XDIST_WORKER_TEARDOWN_TIMEOUT = 600.0

_previous_handlers: dict[int, _SignalHandler] = {}
_interrupting_signum: int | None = None
_session_cleanup_running = False
_config: pytest.Config | None = None


def install_interrupt_handlers(config: pytest.Config) -> None:
    """Replace SIGINT and SIGTERM with a handler that interrupts pytest instead of ending it.

    The session store is not captured here: the teardown runs on the session-finish path, which
    resolves the store itself, so an interrupt during the earliest session fixtures still tears down
    everything they created.

    Args:
        config (pytest.Config): Config of the running session, used to stop the xdist workers.
    """
    global _config
    _config = config
    for _signum in (signal.SIGINT, signal.SIGTERM):
        _previous_handlers[_signum] = signal.signal(_signum, _handle_signal)


def restore_interrupt_handlers() -> None:
    """Put back the handlers that :func:`install_interrupt_handlers` replaced and forget the signal.

    Clearing the signal here means a later pytest session in the same process starts with nothing
    pending instead of re-raising the previous session's signal in its own unconfigure.
    """
    global _interrupting_signum
    while _previous_handlers:
        _signum, _previous_handler = _previous_handlers.popitem()
        signal.signal(_signum, _previous_handler)
    _interrupting_signum = None


@contextmanager
def session_cleanup() -> Iterator[None]:
    """Mark the session cleanup as running, so its first signal does not raise into it.

    While the cleanup runs, :func:`_handle_signal` only records the signal: raising here would skip
    the rest of the cleanup, which is what deletes the session's resources. The recorded signal is
    propagated once the session is done, from ``pytest_unconfigure``.
    """
    global _session_cleanup_running
    _session_cleanup_running = True
    try:
        yield
    finally:
        _session_cleanup_running = False


def interrupting_signum() -> int | None:
    """Return the signal that interrupted this session, or None when it was not interrupted."""
    return _interrupting_signum


def _handle_signal(signum: int, _frame: FrameType | None) -> None:
    """Record the signal and interrupt pytest so its shutdown still runs the tracked teardown."""
    global _interrupting_signum
    if _session_cleanup_running:
        # A second signal still aborts: it raises into the cleanup that has not finished yet.
        if _interrupting_signum is not None:
            raise KeyboardInterrupt(f"Interrupted by signal {signum} during session teardown")
        _interrupting_signum = signum
        LOGGER.warning(
            f"Session interrupted by signal {signum} while the session teardown is running; "
            "finishing it before the signal is propagated"
        )
        return
    LOGGER.warning(f"Session interrupted by signal {signum}, running session teardown")
    _stop_xdist_workers()
    # Restored before the interrupt: a second signal aborts the running teardown instead of
    # queueing another interrupted shutdown. Set after the restore, which clears the record.
    restore_interrupt_handlers()
    _interrupting_signum = signum
    raise KeyboardInterrupt(f"Interrupted by signal {signum}")


def _stop_xdist_workers() -> None:
    """Ask the xdist workers of a controlling session to stop and wait for their own teardown.

    Each worker then runs its own tracked teardown, which holds its own clone records, instead of
    dying with the controller. The controller keeps handling worker completion events until they
    are all finished, bounded by :data:`_XDIST_WORKER_TEARDOWN_TIMEOUT`, because its own session
    finish terminates any worker still running after a short exit timeout.
    """
    if _config is None:
        return
    dsession = _config.pluginmanager.get_plugin("dsession")
    # Before the run loop starts there is no scheduler to shut down and the workers' stores are empty.
    if not isinstance(dsession, DSession) or dsession.sched is None:
        return
    dsession.triggershutdown()
    _wait_for_xdist_workers(dsession=dsession)


def _wait_for_xdist_workers(dsession: DSession) -> None:
    """Handle worker completion events until the stopping workers have all finished.

    ``DSession.loop_once`` is not reused: it blocks until an event arrives, which cannot be bounded
    when a worker hangs.

    Args:
        dsession (DSession): Controlling session whose workers are stopping.
    """
    deadline = time.monotonic() + _XDIST_WORKER_TEARDOWN_TIMEOUT
    LOGGER.info(f"Waiting up to {_XDIST_WORKER_TEARDOWN_TIMEOUT:.0f}s for xdist workers to finish their teardown")
    while not dsession.session_finished:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            LOGGER.warning(
                f"Timed out after {_XDIST_WORKER_TEARDOWN_TIMEOUT:.0f}s waiting for xdist workers to finish teardown"
            )
            return
        try:
            callname, kwargs = dsession.queue.get(timeout=min(remaining, 1.0))
        except Empty:
            continue
        getattr(dsession, f"worker_{callname}")(**kwargs)
    LOGGER.info("xdist workers finished their teardown")
