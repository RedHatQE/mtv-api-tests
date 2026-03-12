from __future__ import annotations

import shlex
import ssl
import urllib.request
from typing import TYPE_CHECKING, Any

from pyVmomi import vim
from simple_logger.logger import get_logger
from timeout_sampler import TimeoutSampler

from exceptions.exceptions import GuestCommandError

if TYPE_CHECKING:
    from libs.providers.vmware import VMWareProvider

LOGGER = get_logger(__name__)

_FILE_TRANSFER_TIMEOUT: int = 30


def run_command_in_vmware_guest(
    content: vim.ServiceInstanceContent,
    vm: vim.VirtualMachine,
    auth: vim.vm.guest.GuestAuthentication,
    command: str,
    vcenter_host: str,
    timeout: int = 30,
) -> str:
    """Execute a command inside a VMware guest via Guest Operations API.

    Runs a shell command inside the guest using vCenter Guest Operations
    (no SSH needed). Auto-detects guest OS via VMware Tools and uses the
    appropriate shell (/bin/bash for Linux, cmd.exe for Windows).
    Handles the full lifecycle: temp file creation, command execution,
    output retrieval, and cleanup.

    Args:
        content (vim.ServiceInstanceContent): vCenter service instance content
        vm (vim.VirtualMachine): VMware VM object to run the command in
        auth (vim.vm.guest.GuestAuthentication): Guest authentication credentials
        command (str): Shell command to execute (must be valid for the guest OS)
        vcenter_host (str): vCenter hostname for URL fixup in file transfers
        timeout (int): Maximum seconds to wait for command completion

    Returns:
        str: Command stdout/stderr output

    Raises:
        GuestCommandError: If the command exits with a non-zero exit code
        ValueError: If guest OS family cannot be determined or is unsupported
        TimeoutExpiredError: If the command does not complete within timeout
        vim.fault.GuestOperationsFault: If guest operations fail (auth, temp file, process)
        urllib.error.URLError: If file transfer from guest fails
    """
    pm = content.guestOperationsManager.processManager
    fm = content.guestOperationsManager.fileManager

    guest_family = getattr(vm.guest, "guestFamily", None)
    if not guest_family:
        raise ValueError(f"Cannot determine guest OS for VM {vm.name}. Ensure VMware Tools is installed and running.")

    if guest_family == "linuxGuest":
        temp_dir = "/tmp"
        program_path = "/bin/bash"
    elif guest_family == "windowsGuest":
        temp_dir = "C:\\Windows\\Temp"
        program_path = "C:\\Windows\\System32\\cmd.exe"
    else:
        raise ValueError(f"Unsupported guest OS family '{guest_family}' for VM {vm.name}")

    output_file = fm.CreateTemporaryFileInGuest(
        vm=vm, auth=auth, prefix="mtv_guest_cmd_", suffix=".txt", directoryPath=temp_dir
    )

    try:
        redirect = f"{command} > {output_file} 2>&1"
        if guest_family == "linuxGuest":
            arguments = f"-c {shlex.quote(redirect)}"
        else:
            arguments = f'/c "{redirect}"'

        pid = pm.StartProgramInGuest(
            vm=vm,
            auth=auth,
            spec=vim.vm.guest.ProcessManager.ProgramSpec(
                programPath=program_path,
                arguments=arguments,
            ),
        )

        for process_info in TimeoutSampler(
            wait_timeout=timeout,
            sleep=0.5,
            func=lambda: next(
                (p for p in pm.ListProcessesInGuest(vm=vm, auth=auth, pids=[pid]) if p.endTime),
                None,
            ),
        ):
            if process_info:
                break

        if process_info.exitCode != 0:
            raise GuestCommandError(f"Guest process {pid} on VM {vm.name} exited with code {process_info.exitCode}")

        transfer_info = fm.InitiateFileTransferFromGuest(vm=vm, auth=auth, guestFilePath=output_file)
        url = transfer_info.url
        if "://*" in url:
            url = url.replace("://*", f"://{vcenter_host}")

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(url, context=ctx, timeout=_FILE_TRANSFER_TIMEOUT) as resp:
            return resp.read().decode("utf-8")
    finally:
        try:
            fm.DeleteFileInGuest(vm=vm, auth=auth, filePath=output_file)
        except Exception:
            LOGGER.debug(f"Failed to clean up temp file {output_file} in guest VM {vm.name}", exc_info=True)


def _parse_nmcli_ip_origins(output: str) -> dict[str, bool]:
    """Parse nmcli IP origin output into a mapping of IP addresses to static IP flags.

    Args:
        output (str): Raw output from nmcli command, with lines in 'IP|method' format

    Returns:
        dict[str, bool]: Mapping of IP address to whether it's a static IP (method == 'manual')
    """
    origins: dict[str, bool] = {}
    for line in output.strip().splitlines():
        if "|" in line:
            ip_addr, method = line.split("|", 1)
            origins[ip_addr.strip()] = method.strip() == "manual"
    return origins


def _apply_ip_origins_to_vm_details(
    vm_details: dict[str, Any],
    origins: dict[str, bool],
    vm_name: str,
) -> None:
    """Apply detected IP origins to VM details network interface data.

    Updates vm_details in-place, setting is_static_ip and ip_origin for IPs
    where the origin was previously unknown (None).

    Args:
        vm_details (dict[str, Any]): VM details dict with network_interfaces data
        origins (dict[str, bool]): Mapping of IP address to static IP flag
        vm_name (str): VM name for logging
    """
    for nic in vm_details.get("network_interfaces", []):
        for ip_config in nic.get("ip_addresses", []):
            if ip_config.get("is_static_ip") is not None:
                continue
            ip_addr = ip_config.get("ip_address", "")
            if ip_addr in origins:
                ip_config["is_static_ip"] = origins[ip_addr]
                ip_config["ip_origin"] = "manual" if origins[ip_addr] else "auto"
                LOGGER.info(f"VM {vm_name}: IP {ip_addr} origin={ip_config['ip_origin']} (via Guest Operations)")


def detect_vmware_ip_origins_via_guest_ops(
    source_provider: VMWareProvider,
    vm: vim.VirtualMachine,
    source_provider_data: dict[str, Any],
    vm_details: dict[str, Any],
) -> None:
    """Detect IP assignment method for Linux guests via VMware Guest Operations API.

    Workaround for open-vm-tools not reporting IP origin on Linux guests
    (https://github.com/vmware/open-vm-tools/issues/694).
    Runs nmcli inside the guest via vCenter Guest Operations (no SSH needed).

    Updates vm_details in-place with detected IP origins.

    Args:
        source_provider (VMWareProvider): VMware provider instance with `content` and `host` attributes
        vm (vim.VirtualMachine): VMware VM object
        source_provider_data (dict[str, Any]): Provider config containing guest credentials
        vm_details (dict[str, Any]): VM details dict from vm_dict(), updated in-place

    Raises:
        ValueError: If guest credentials are not found in provider config
        GuestCommandError: If the guest command exits with a non-zero exit code
        TimeoutExpiredError: If the guest process does not complete within 30 seconds
        vim.fault.GuestOperationsFault: If guest operations fail (auth, temp file, process)
        urllib.error.URLError: If file transfer from guest fails
    """
    try:
        guest_username = source_provider_data["guest_vm_linux_user"]
        guest_password = source_provider_data["guest_vm_linux_password"]
    except KeyError as e:
        raise ValueError(
            f"Linux VM credentials not found in provider config: {e}. "
            "Required: guest_vm_linux_user, guest_vm_linux_password"
        ) from e

    LOGGER.info(f"Detecting IP origins via Guest Operations for VM {vm.name}")

    has_unknown_origins = any(
        ip.get("is_static_ip") is None
        for nic in vm_details.get("network_interfaces", [])
        for ip in nic.get("ip_addresses", [])
    )
    if not has_unknown_origins:
        LOGGER.info(f"All IP origins already known for VM {vm.name}, skipping Guest Operations detection")
        return

    auth = vim.vm.guest.NamePasswordAuthentication(
        username=guest_username, password=guest_password, interactiveSession=False
    )

    script = (
        "for uuid in $(nmcli -t -f UUID connection show --active); do "
        'dev=$(nmcli -g GENERAL.DEVICES connection show "$uuid" 2>/dev/null); '
        'test -z "$dev" -o "$dev" = lo && continue; '
        'method=$(nmcli -g ipv4.method connection show "$uuid" 2>/dev/null); '
        'test -z "$method" && continue; '
        'ip -4 -o addr show "$dev" 2>/dev/null | while read -r _ _ _ cidr _; do '
        'printf "%s|%s\\n" "${cidr%%/*}" "$method"; '
        "done; done"
    )

    vcenter_host = source_provider.host
    if vcenter_host is None:
        raise ValueError(f"vCenter host not available for provider used by VM {vm.name}")

    try:
        output = run_command_in_vmware_guest(
            content=source_provider.content,
            vm=vm,
            auth=auth,
            command=script,
            vcenter_host=vcenter_host,
        )
    except GuestCommandError:
        LOGGER.warning(f"Guest command failed for VM {vm.name}, IP origin detection incomplete")
        raise

    origins = _parse_nmcli_ip_origins(output)
    if not origins:
        LOGGER.warning(f"No IP origin data detected via Guest Operations for VM {vm.name}")
        return

    _apply_ip_origins_to_vm_details(vm_details=vm_details, origins=origins, vm_name=vm.name)
