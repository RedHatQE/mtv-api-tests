"""
SSH utilities for connecting to migrated VMs on OpenShift.
Uses virtctl port-forward approach combined with python-rrmngmnt for SSH operations.
This works regardless of whether cluster nodes have external IP addresses.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
from types import TracebackType
from typing import Any

from kubernetes.dynamic import DynamicClient
from ocp_resources.virtual_machine import VirtualMachine
from pytest_testconfig import config as py_config
from rrmngmnt import Host, RootUser, User, UserWithPKey
from simple_logger.logger import get_logger
from timeout_sampler import TimeoutSampler, TimeoutExpiredError

from libs.base_provider import BaseProvider

LOGGER = get_logger(__name__)


class VMSSHConnection:
    """SSH connection wrapper for VMs running on OpenShift/KubeVirt using python-rrmngmnt."""

    def __init__(
        self,
        vm: VirtualMachine,
        username: str,
        password: str | None = None,
        private_key_path: str | None = None,
        port: int = 22,
        ocp_token: str | None = None,
        ocp_api_server: str | None = None,
        ocp_insecure: bool = False,
    ):
        self.vm = vm
        self.username = username
        self.password = password
        self.private_key_path = private_key_path
        self.port = port
        self.ocp_token = ocp_token
        self.ocp_api_server = ocp_api_server
        self.ocp_insecure = ocp_insecure
        self.port_forward_process: subprocess.Popen[str] | None = None
        self.local_port: int | None = None
        self.rrmngmnt_host: Host | None = None
        self.rrmngmnt_user: User | None = None

    def _test_ssh_connectivity(self, host: str, port: int) -> bool:
        """Test if SSH connection is actually working using rrmngmnt."""
        try:
            # Create a temporary host for testing
            test_host = Host(host)

            # Configure user authentication
            if self.private_key_path:
                user = UserWithPKey(self.username, self.private_key_path)
            elif self.username == "root":
                user = RootUser(self.password)
            else:
                user = User(self.username, self.password)

            # Set the port and test connectivity using the recommended method
            executor = test_host.executor(user=user)
            executor.port = port

            # Use executor's connectivity test (recommended approach)
            return executor.is_connective(tcp_timeout=5.0)

        except Exception as e:
            LOGGER.debug(f"SSH connectivity test failed: {e}")
            return False

    def __enter__(self) -> VMSSHConnection:
        self.connect()
        return self

    def __exit__(self, exc_type: type[object] | None, exc_val: object | None, exc_tb: TracebackType | None) -> None:
        self.disconnect()

    def setup_port_forward(self, local_port: int | None = None, max_retries: int = 5) -> int:
        """
        Set up virtctl port-forward for SSH access to the VM.

        Args:
            local_port: Local port to bind to. If None, a random available port is used.
            max_retries: Maximum number of retry attempts for establishing port-forward. Default is 3.

        Returns:
            The local port number that was bound.
        """
        # Get virtctl binary path
        virtctl_path = shutil.which("virtctl")
        if not virtctl_path:
            raise RuntimeError(
                "virtctl command not found in PATH. "
                "Please install virtctl before running the test suite. "
                "See README.md for installation instructions."
            )

        # Use a random available port if none specified
        if local_port is None:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("", 0))
                local_port = s.getsockname()[1]

        # Build virtctl port-forward command with token authentication
        cmd = [
            virtctl_path,
            "port-forward",
            f"vm/{self.vm.name}",
            f"{local_port}:22",
            "--namespace",
            self.vm.namespace,
            "--address",
            "127.0.0.1",
        ]

        # Add OCP authentication parameters
        if self.ocp_api_server:
            cmd.extend(["--server", self.ocp_api_server])

        if self.ocp_token:
            cmd.extend(["--token", self.ocp_token])

        if self.ocp_insecure:
            cmd.append("--insecure-skip-tls-verify")

        # Add debug verbosity
        cmd.extend(["-v", "3"])

        cmd_str = " ".join(cmd)
        if self.ocp_token:
            cmd_str = cmd_str.replace(self.ocp_token, "[REDACTED]")
        LOGGER.info(f"Full virtctl command: {cmd_str}")

        # Retry mechanism using TimeoutSampler
        def attempt_port_forward():
            """Single attempt to establish port-forward."""
            try:
                LOGGER.info("Attempting to establish port-forward")

                # Start port-forward in background
                self.port_forward_process = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
                )

                # Give port-forward time to establish
                time.sleep(3)

                # Check if process is still running
                if self.port_forward_process.poll() is not None:
                    stdout, stderr = self.port_forward_process.communicate()
                    LOGGER.warning(f"virtctl process exited early: {stderr}")
                    self.port_forward_process = None
                    return False

                # Test if tunnel actually works
                if self._test_ssh_connectivity("localhost", local_port):
                    # Success! Store the port and return
                    self.local_port = local_port
                    LOGGER.info(
                        f"Port-forward established successfully: localhost:{local_port} -> VM {self.vm.name}:22"
                    )
                    return True
                else:
                    LOGGER.warning("SSH connectivity test failed")
                    # Kill the process
                    if self.port_forward_process:
                        self.port_forward_process.terminate()
                        try:
                            self.port_forward_process.wait(timeout=5)
                        except (subprocess.TimeoutExpired, ProcessLookupError, OSError):
                            self.port_forward_process.kill()
                        self.port_forward_process = None
                    return False
            except (subprocess.SubprocessError, OSError, RuntimeError) as e:
                LOGGER.error(f"Error during port-forward attempt: {e}")
                if self.port_forward_process:
                    try:
                        self.port_forward_process.terminate()
                        self.port_forward_process.wait(timeout=5)
                    except (subprocess.TimeoutExpired, ProcessLookupError, OSError):
                        self.port_forward_process.kill()
                    self.port_forward_process = None
                return False

        try:
            for sample in TimeoutSampler(
                wait_timeout=20 * max_retries,
                sleep=20,
                func=attempt_port_forward,
            ):
                if sample:
                    return local_port
        except TimeoutExpiredError as e:
            raise RuntimeError(f"Failed to establish working port-forward after {max_retries} attempts") from e

        return local_port

    def is_connective(self, tcp_timeout: float = 5.0) -> bool:
        """Check if SSH connection is available using rrmngmnt's built-in method."""
        if not self.rrmngmnt_host:
            raise RuntimeError("SSH connection not established")

        # Use the stored user object (set in connect())
        if self.rrmngmnt_user:
            executor = self.rrmngmnt_host.executor(user=self.rrmngmnt_user)
        else:
            executor = self.rrmngmnt_host.executor()

        executor.port = self.local_port

        # Use executor's is_connective method (not host's, which creates new executor)
        return executor.is_connective(tcp_timeout=tcp_timeout)

    def connect(self) -> Host:
        """Establish SSH connection using virtctl port-forward and rrmngmnt."""
        LOGGER.info(f"Connecting to VM {self.vm.name} via SSH using rrmngmnt")

        if not self.local_port:
            try:
                self.setup_port_forward()
            except RuntimeError as e:
                raise RuntimeError(f"Port-forward setup failed: {e}")

        if self.local_port is None:
            raise RuntimeError("Port-forward failed to establish")

        self.rrmngmnt_host = Host("localhost")

        # Configure user authentication
        if self.private_key_path:
            user = UserWithPKey(self.username, self.private_key_path)
        elif self.username == "root":
            user = RootUser(self.password)
        else:
            user = User(self.username, self.password)

        self.rrmngmnt_host.users.append(user)
        self.rrmngmnt_user = user

        connected = False
        try:
            executor = self.rrmngmnt_host.executor(user=user)
            executor.port = self.local_port

            # rrmngmnt's run_cmd() always returns a tuple: (rc, out, err)
            rc, out, err = executor.run_cmd(["echo", "test"])
            if rc != 0:
                raise RuntimeError(f"Connection test failed: {err}")

            LOGGER.info(f"SSH connection established to VM {self.vm.name} via localhost:{self.local_port}")
            connected = True
            return self.rrmngmnt_host

        except (RuntimeError, OSError, ConnectionError) as e:
            LOGGER.error(f"Failed to establish SSH connection to VM {self.vm.name}: {e}")
            raise
        finally:
            if not connected:
                self.disconnect()

    def disconnect(self) -> None:
        """Close SSH connection and clean up resources."""
        if self.rrmngmnt_host:
            self.rrmngmnt_host = None
            self.rrmngmnt_user = None
            LOGGER.info(f"SSH connection to VM {self.vm.name} closed")

        if hasattr(self, "port_forward_process") and self.port_forward_process:
            try:
                self.port_forward_process.terminate()
                self.port_forward_process.wait(timeout=5)
                LOGGER.info(f"Port-forward process for VM {self.vm.name} terminated")
            except (subprocess.TimeoutExpired, ProcessLookupError, OSError) as e:
                LOGGER.warning(f"Error terminating port-forward process: {e}")
                try:
                    self.port_forward_process.kill()
                except (ProcessLookupError, OSError):
                    # Process already terminated, ignore
                    pass
            finally:
                self.port_forward_process = None

        # Reset local_port so that reconnection will set up a new port-forward
        if self.local_port is not None:
            LOGGER.debug(f"Resetting local_port to None (was {self.local_port})")
            self.local_port = None

    def get_rrmngmnt_host(self) -> Host:
        """Get the underlying rrmngmnt Host object for direct access to its API."""
        if not self.rrmngmnt_host:
            raise RuntimeError("SSH connection not established")

        executor = self.rrmngmnt_host.executor()
        executor.port = self.local_port
        return self.rrmngmnt_host


def create_vm_ssh_connection(
    vm: VirtualMachine,
    username: str,
    password: str | None = None,
    private_key_path: str | None = None,
    ocp_token: str | None = None,
    ocp_api_server: str | None = None,
    ocp_insecure: bool = False,
    **kwargs: Any,
) -> VMSSHConnection:
    """
    Factory function to create SSH connection to a VM using OCP token authentication.
    """
    return VMSSHConnection(
        vm=vm,
        username=username,
        password=password,
        private_key_path=private_key_path,
        ocp_token=ocp_token,
        ocp_api_server=ocp_api_server,
        ocp_insecure=ocp_insecure,
        **kwargs,
    )


class SSHConnectionManager:
    """
    Manages SSH connections to migrated VMs for test fixtures.

    Usage:
        manager = SSHConnectionManager(
            provider=destination_provider,
            namespace=target_namespace,
            fixture_store=fixture_store,
            ocp_client=ocp_admin_client,
        )
        ssh_conn = manager.create(vm_name="my-vm", username="root", password="pass")
        with ssh_conn:
            from pyhelper_utils.shell import run_ssh_commands
            results = run_ssh_commands(ssh_conn.rrmngmnt_host, ["whoami"])
        manager.cleanup_all()
    """

    def __init__(
        self,
        provider: BaseProvider,
        namespace: str,
        fixture_store: dict[str, Any],
        ocp_client: DynamicClient,
    ) -> None:
        self.provider = provider
        self.namespace = namespace
        self.fixture_store = fixture_store
        self.connections: list[VMSSHConnection] = []

        # Extract OCP credentials from the client
        self.ocp_api_server = ocp_client.configuration.host

        # Get insecure_verify_skip from config and convert to boolean if needed
        insecure_config = py_config.get("insecure_verify_skip")
        if isinstance(insecure_config, str):
            self.ocp_insecure = insecure_config.lower() == "true"
        else:
            self.ocp_insecure = bool(insecure_config) if insecure_config else False

        # Store the client reference for on-demand token extraction
        self._ocp_client = ocp_client

    @property
    def ocp_token(self) -> str | None:
        """Extract OCP token on-demand to minimize exposure window."""
        try:
            api_key = self._ocp_client.configuration.api_key.get("authorization")
            if api_key:
                return api_key.split()[-1]
        except (AttributeError, KeyError, IndexError):
            pass
        return None

    def create(
        self,
        vm_name: str,
        username: str,
        password: str | None = None,
        private_key_path: str | None = None,
        **kwargs: Any,
    ) -> VMSSHConnection:
        """Create and track an SSH connection to a VM."""
        ssh_conn = self.provider.create_ssh_connection_to_vm(  # type: ignore[attr-defined]
            vm_name=vm_name,
            namespace=self.namespace,
            username=username,
            password=password,
            private_key_path=private_key_path,
            ocp_token=self.ocp_token,
            ocp_api_server=self.ocp_api_server,
            ocp_insecure=self.ocp_insecure,
            **kwargs,
        )

        # Track connection for cleanup
        self.connections.append(ssh_conn)

        LOGGER.info(f"Created SSH connection to VM {vm_name}")
        return ssh_conn

    def cleanup_all(self) -> None:
        """Clean up all SSH connections and services."""
        for ssh_conn in self.connections:
            try:
                ssh_conn.disconnect()
            except Exception as e:
                LOGGER.warning(f"Error cleaning up SSH connection: {e}")

        self.connections.clear()
        LOGGER.info("Cleaned up all SSH connections")
