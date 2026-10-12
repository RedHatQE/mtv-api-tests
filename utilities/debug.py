import bdb
import ipaddress
import os
import socket
import sys

from remote_pdb import RemotePdb

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


def _remote_debugger() -> RemotePdb:
    """Create a remote debugger listener and wait for a client connection.

    Blocks until a debugger client (nc/telnet) connects. The listener binds loopback
    by default so the debugger is never exposed on the cluster network; pod runs
    attach via oc port-forward. The port is offset per pytest-xdist worker so
    parallel workers listen on distinct ports.

    Returns:
        RemotePdb: Debugger instance connected to a client.

    Raises:
        ValueError: If REMOTE_PDB_HOST is empty, unresolvable, or not a loopback
            address, or REMOTE_PDB_PORT is not an integer. Loopback-only binding is
            enforced so the unauthenticated debugger is never exposed on the cluster
            network.
    """
    host = os.environ.get("REMOTE_PDB_HOST", _DEFAULT_REMOTE_DEBUGGER_HOST)
    if not host:
        raise ValueError("REMOTE_PDB_HOST must not be empty")
    try:
        addrinfos = socket.getaddrinfo(host, None, family=socket.AF_INET)
    except socket.gaierror as err:
        raise ValueError(f"REMOTE_PDB_HOST '{host}' does not resolve: {err}") from err
    # Bind a validated numeric loopback address, not the hostname: RemotePdb would
    # re-resolve a hostname at bind time (second resolution may differ), and its
    # socket is IPv4-only.
    loopback_addresses: list[str] = []
    for _, _, _, _, sockaddr in addrinfos:
        address = sockaddr[0]
        if isinstance(address, str) and ipaddress.ip_address(address).is_loopback:
            loopback_addresses.append(address)
    if not loopback_addresses:
        raise ValueError(f"REMOTE_PDB_HOST must resolve to a loopback address, got '{host}'")
    host = loopback_addresses[0]

    port_str = os.environ.get("REMOTE_PDB_PORT", str(_DEFAULT_REMOTE_DEBUGGER_PORT))
    try:
        base_port = int(port_str)
    except ValueError as err:
        raise ValueError(f"REMOTE_PDB_PORT must be an integer, got '{port_str}'") from err

    worker = os.environ.get("PYTEST_XDIST_WORKER", "")
    worker_index = int(worker[2:]) if worker.startswith("gw") and worker[2:].isdigit() else 0
    return RemotePdb(host=host, port=base_port + worker_index)


def remote_breakpoint() -> None:
    """Block and wait for a remote debugger connection at the call site.

    No-op unless MTV_REMOTE_PDB is set, so calls added during a debugging session
    never affect normal runs. Intended for temporary, uncommitted use: place the
    call at the line of interest, run with MTV_REMOTE_PDB=1 (and -s so the
    listening message is not captured), attach with `nc <host> <port>`, and remove
    the call when done.

    Raises:
        bdb.BdbQuit: If the debugger client quits the session; the failure
            propagates and the current test fails while the run continues.
        ValueError: If the debugger host/port configuration is malformed.
    """
    if not remote_debugger_enabled():
        return
    try:
        _remote_debugger().set_trace(frame=sys._getframe().f_back)
    except (ConnectionResetError, BrokenPipeError) as err:
        raise bdb.BdbQuit(f"Remote debugger connection lost: {err}") from err


def remote_post_mortem(exception: BaseException) -> None:
    """Open a remote post-mortem debugger session on a raised exception.

    No-op unless MTV_REMOTE_PDB is set. Blocks until a client connects
    (`nc <host> <port>`), then drops into a post-mortem pdb on the exception's
    traceback.

    Args:
        exception (BaseException): The raised exception to debug.

    Raises:
        bdb.BdbQuit: If the debugger client quits the session or disconnects; the
            caller decides how to handle it (the conftest hook ends the test session).
        ValueError: If the debugger host/port configuration is malformed.
    """
    if not remote_debugger_enabled():
        return
    debugger = _remote_debugger()
    debugger.reset()
    try:
        debugger.interaction(None, exception.__traceback__)
    except (ConnectionResetError, BrokenPipeError) as err:
        raise bdb.BdbQuit(f"Remote debugger connection lost: {err}") from err
    if debugger.quitting:
        raise bdb.BdbQuit("Remote debugger session was quit or disconnected")
