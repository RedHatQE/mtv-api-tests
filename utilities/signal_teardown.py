"""Turn SIGINT/SIGTERM into pytest's interrupted shutdown, so the tracked teardown still runs.

Issue #842: teardown only ran on the clean session-finish path, so an interrupted run left its
clones and other resources behind on the lab. A handler installed at session start records the
signal and interrupts pytest, which unwinds through the normal session-finish path where the
tracked teardown lives. Ending the process from the handler instead would skip that teardown
entirely, and on an xdist controller it would kill the workers before they run theirs.
"""

from __future__ import annotations

import signal
from collections.abc import Callable
from types import FrameType

import pytest
from simple_logger.logger import get_logger
from xdist.dsession import DSession

LOGGER = get_logger(__name__)

# signal.signal returns the handler it replaced: a callable, SIG_DFL/SIG_IGN, or None.
_SignalHandler = Callable[[int, FrameType | None], object] | int | None

_previous_handlers: dict[int, _SignalHandler] = {}
_interrupting_signum: int | None = None
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
    """Put back the handlers that :func:`install_interrupt_handlers` replaced."""
    while _previous_handlers:
        _signum, _previous_handler = _previous_handlers.popitem()
        signal.signal(_signum, _previous_handler)


def interrupting_signum() -> int | None:
    """Return the signal that interrupted this session, or None when it was not interrupted."""
    return _interrupting_signum


def _handle_signal(signum: int, _frame: FrameType | None) -> None:
    """Record the signal and interrupt pytest so its shutdown still runs the tracked teardown."""
    global _interrupting_signum
    LOGGER.warning(f"Session interrupted by signal {signum}, running session teardown")
    _interrupting_signum = signum
    _stop_xdist_workers()
    # Restored before the interrupt: a second signal aborts the running teardown instead of
    # queueing another interrupted shutdown.
    restore_interrupt_handlers()
    raise KeyboardInterrupt(f"Interrupted by signal {signum}")


def _stop_xdist_workers() -> None:
    """Ask the xdist workers of a controlling session to stop after their current test.

    Each worker then runs its own tracked teardown, which holds its own clone records, instead of
    dying with the controller.
    """
    if _config is None:
        return
    dsession = _config.pluginmanager.get_plugin("dsession")
    # Before the run loop starts there is no scheduler to shut down and the workers' stores are empty.
    if isinstance(dsession, DSession) and dsession.sched is not None:
        dsession.triggershutdown()
