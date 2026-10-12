import bdb
import logging
import os
import sys

from remote_pdb import RemotePdb

LOGGER = logging.getLogger(__name__)

# Default debugger listening port; pytest-xdist workers get an offset (gw1 -> +1) so
# simultaneous debugger sessions in parallel workers do not collide on the same port.
_DEFAULT_REMOTE_DEBUGGER_PORT = 4444
_DEFAULT_REMOTE_DEBUGGER_HOST = "127.0.0.1"


def remote_debugger_enabled() -> bool:
    """Check whether remote debugging is enabled for this run.

    Returns:
        bool: True when the MTV_REMOTE_PDB environment variable is set.
    """
    return bool(os.environ.get("MTV_REMOTE_PDB"))


def _remote_debugger_host() -> str:
    """Resolve the debugger listening host.

    Returns:
        str: REMOTE_PDB_HOST value, defaulting to loopback so the debugger is never
            exposed on the cluster network (pod runs attach via oc port-forward).
    """
    return os.environ.get("REMOTE_PDB_HOST", _DEFAULT_REMOTE_DEBUGGER_HOST)


def _remote_debugger_port() -> int:
    """Resolve the debugger listening port, offset per pytest-xdist worker.

    Returns:
        int: REMOTE_PDB_PORT value (default 4444) plus the numeric part of the
            PYTEST_XDIST_WORKER id, so parallel workers listen on distinct ports.
    """
    base_port = int(os.environ.get("REMOTE_PDB_PORT", str(_DEFAULT_REMOTE_DEBUGGER_PORT)))
    worker = os.environ.get("PYTEST_XDIST_WORKER", "")
    worker_index = int(worker[2:]) if worker.startswith("gw") and worker[2:].isdigit() else 0
    return base_port + worker_index


def remote_breakpoint() -> None:
    """Block and wait for a remote debugger connection at the call site.

    No-op unless MTV_REMOTE_PDB is set, so calls added during a debugging session
    never affect normal runs. Intended for temporary, uncommitted use: place the
    call at the line of interest, run with MTV_REMOTE_PDB=1 (and -s so the
    listening message is not captured), attach with `nc <host> <port>`, and remove
    the call when done.

    Raises:
        None: BdbQuit from a quitting debugger session is caught and logged.
    """
    if not remote_debugger_enabled():
        return
    try:
        RemotePdb(host=_remote_debugger_host(), port=_remote_debugger_port()).set_trace(frame=sys._getframe().f_back)
    except bdb.BdbQuit:
        LOGGER.warning("Remote debugger session quit; continuing execution.")


def remote_post_mortem(exception: BaseException) -> None:
    """Open a remote post-mortem debugger session on a raised exception.

    No-op unless MTV_REMOTE_PDB is set. Blocks until a client connects
    (`nc <host> <port>`), then drops into a post-mortem pdb on the exception.

    Args:
        exception (BaseException): The raised exception to debug.

    Raises:
        None: BdbQuit from a quitting debugger session is caught and logged.
    """
    if not remote_debugger_enabled():
        return
    try:
        debugger = RemotePdb(host=_remote_debugger_host(), port=_remote_debugger_port())
        debugger.reset()
        debugger.interaction(None, exception)
    except bdb.BdbQuit:
        LOGGER.warning("Remote debugger session quit; continuing test run.")
