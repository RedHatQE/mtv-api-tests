from __future__ import annotations

import json
import re
import shlex
import ssl
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any, TypedDict
from uuid import uuid4

from pyVmomi import vim
from simple_logger.logger import get_logger
from timeout_sampler import TimeoutExpiredError, TimeoutSampler

from exceptions.exceptions import GuestCommandError

if TYPE_CHECKING:
    from libs.providers.vmware import VMWareProvider

LOGGER = get_logger(__name__)

_FILE_TRANSFER_TIMEOUT: int = 30
_GUEST_COMMAND_CLEANUP_TIMEOUT: int = 10
_GUEST_OS_READ_TIMEOUT_SECONDS: int = 12
_GUEST_OS_READ_KILL_AFTER_SECONDS: int = 3
_GUEST_MOUNT_CLEANUP_TIMEOUT_SECONDS: int = 3
_GUEST_MOUNT_CLEANUP_KILL_AFTER_SECONDS: int = 1
_GUEST_CLEANUP_COMMAND_TIMEOUT_SECONDS: int = 1
_GUEST_CLEANUP_COMMAND_KILL_AFTER_SECONDS: int = 1
_GUEST_OS_READ_TIMEOUT_COMMAND = (
    f"timeout --signal=TERM --kill-after={_GUEST_OS_READ_KILL_AFTER_SECONDS}s {_GUEST_OS_READ_TIMEOUT_SECONDS}s"
)
_GUEST_MOUNT_CLEANUP_TIMEOUT_COMMAND = (
    f"timeout --signal=TERM --kill-after={_GUEST_MOUNT_CLEANUP_KILL_AFTER_SECONDS}s "
    f"{_GUEST_MOUNT_CLEANUP_TIMEOUT_SECONDS}s"
)
_GUEST_CLEANUP_COMMAND_TIMEOUT = (
    f"timeout --signal=TERM --kill-after={_GUEST_CLEANUP_COMMAND_KILL_AFTER_SECONDS}s "
    f"{_GUEST_CLEANUP_COMMAND_TIMEOUT_SECONDS}s"
)
DATA_INTEGRITY_DIR: str = "/opt/mtv_data_integrity"
DATA_INTEGRITY_FILE: str = f"{DATA_INTEGRITY_DIR}/marker.txt"
_SUPPORTED_ROOT_FILESYSTEMS = {"ext2", "ext3", "ext4", "xfs"}
_READ_ONLY_MOUNT_OPTIONS = {
    "ext2": "ro",
    "ext3": "ro,noload",
    "ext4": "ro,noload",
    "xfs": "ro,norecovery,nouuid",
}
# Forklift's vSphere rootDisk mapper derives the disk index from one letter after /dev/sd.
_SUPPORTED_ROOT_DEVICE = re.compile(r"^/dev/sd[a-z][0-9]+$")


class LinuxOSRoot(TypedDict):
    """A Linux OS installation and the source partition containing its root filesystem."""

    root_device: str
    os_id: str
    version_id: str


class LinuxBlockDevice(TypedDict):
    """A flattened lsblk record for a guest block device."""

    path: str
    type: str
    fstype: str
    mountpoint: str | None


def parse_linux_os_release(output: str) -> dict[str, str]:
    """Extract the Linux distribution ID and version from os-release content.

    Args:
        output: Contents of an os-release file.

    Returns:
        The parsed distribution ID and version.

    Raises:
        ValueError: If ID or VERSION_ID is missing or malformed.
    """
    values: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"ID", "VERSION_ID"}:
            parsed_value = shlex.split(value)
            if len(parsed_value) != 1:
                raise ValueError(f"Invalid {key} value in os-release: {value!r}")
            values[key] = parsed_value[0]

    if not values.get("ID") or not values.get("VERSION_ID"):
        raise ValueError("os-release must define non-empty ID and VERSION_ID values")

    return {"os_id": values["ID"], "version_id": values["VERSION_ID"]}


def _parse_lsblk_pairs(output: str, vm_name: str) -> list[LinuxBlockDevice]:
    """Parse lsblk's backward-compatible key/value output.

    Args:
        output: Output from lsblk using --pairs.
        vm_name: Source VM name for diagnostics.

    Returns:
        Flattened block-device records.

    Raises:
        ValueError: If a row is malformed or missing required columns.
    """
    block_devices: list[LinuxBlockDevice] = []
    required_columns = {"NAME", "TYPE", "FSTYPE", "MOUNTPOINT"}
    for line in output.splitlines():
        if not line.strip():
            continue
        values: dict[str, str] = {}
        try:
            fields = shlex.split(line)
        except ValueError as error:
            raise ValueError(f"Malformed lsblk output for VM {vm_name}: {line!r}") from error
        for field in fields:
            key, separator, value = field.partition("=")
            if not separator:
                raise ValueError(f"Invalid lsblk output for VM {vm_name}: {line!r}")
            values[key] = re.sub(r"\\x([0-9a-fA-F]{2})", lambda match: chr(int(match[1], 16)), value)
        if not required_columns.issubset(values):
            raise ValueError(f"lsblk output is missing required columns for VM {vm_name}: {line!r}")
        block_devices.append({
            "path": values["NAME"],
            "type": values["TYPE"],
            "fstype": values["FSTYPE"],
            "mountpoint": values["MOUNTPOINT"] or None,
        })
    if not block_devices:
        raise ValueError(f"lsblk returned no block devices for VM {vm_name}")
    return block_devices


def _get_linux_root_inventory(
    source_provider: VMWareProvider,
    vm: vim.VirtualMachine,
    auth: vim.vm.guest.GuestAuthentication,
    vcenter_host: str,
) -> tuple[str, dict[str, str], list[LinuxBlockDevice]]:
    """Read the active Linux root identity and lsblk inventory from a guest.

    Args:
        source_provider: Connected vSphere provider.
        vm: Powered-on Linux VM with VMware Tools running.
        auth: Guest Operations credentials.
        vcenter_host: vCenter hostname used by Guest Operations file transfer.

    Returns:
        The active root path, its OS identity, and lsblk block-device entries.

    Raises:
        ValueError: If the guest root or block-device inventory is unsupported.
        GuestCommandError: If a command inside the guest fails.
        TimeoutExpiredError: If a guest command does not complete within its timeout.
        vim.fault.GuestOperationsFault: If VMware Guest Operations fails.
        urllib.error.URLError: If Guest Operations output cannot be retrieved.
    """
    guest_details = run_command_in_vmware_guest(
        content=source_provider.content,
        vm=vm,
        auth=auth,
        command=('set -e; id -u; readlink -f "$(findmnt -n -o SOURCE /)"; cat /etc/os-release'),
        vcenter_host=vcenter_host,
    )
    block_devices_output = run_command_in_vmware_guest(
        content=source_provider.content,
        vm=vm,
        auth=auth,
        command="lsblk --pairs --paths --noheadings --output NAME,TYPE,FSTYPE,MOUNTPOINT",
        vcenter_host=vcenter_host,
    )

    guest_detail_lines = guest_details.splitlines()
    if len(guest_detail_lines) < 4:
        raise ValueError(f"Guest root or os-release information is missing for VM {vm.name}")
    if guest_detail_lines[0] != "0":
        raise ValueError(f"Root OS discovery requires root Guest Operations credentials for VM {vm.name}")

    active_root_device = guest_detail_lines[1]
    active_os = parse_linux_os_release("\n".join(guest_detail_lines[2:]))
    if not _SUPPORTED_ROOT_DEVICE.fullmatch(active_root_device):
        raise ValueError(
            f"Unsupported active root device {active_root_device!r} for VM {vm.name}; "
            "expected a partition path such as /dev/sda2"
        )

    return active_root_device, active_os, _parse_lsblk_pairs(block_devices_output, vm.name)


def _read_linux_os_release_from_partition(
    source_provider: VMWareProvider,
    vm: vim.VirtualMachine,
    auth: vim.vm.guest.GuestAuthentication,
    vcenter_host: str,
    device_path: str,
    filesystem: str,
    mountpoint: str | None,
) -> dict[str, str] | None:
    """Read os-release from a partition, mounting it read-only when needed.

    Args:
        source_provider: Connected vSphere provider.
        vm: Source VM inspected through Guest Operations.
        auth: Guest Operations credentials.
        vcenter_host: vCenter hostname used by Guest Operations file transfer.
        device_path: Linux block partition path.
        filesystem: Partition filesystem type.
        mountpoint: Existing guest mountpoint, if the partition is mounted.

    Returns:
        Parsed OS identity, or None when the partition does not contain os-release.

    Raises:
        ValueError: If os-release is malformed.
        GuestCommandError: If a guest command or partition mount fails.
        TimeoutExpiredError: If a guest command does not complete within its timeout.
        vim.fault.GuestOperationsFault: If VMware Guest Operations fails.
        urllib.error.URLError: If Guest Operations output cannot be retrieved.
    """
    timeout_cleanup_command: str | None = None
    if mountpoint:
        os_release_path = f"{mountpoint.rstrip('/')}/etc/os-release"
        command = (
            f"if [ -r {shlex.quote(os_release_path)} ]; then "
            f"{_GUEST_OS_READ_TIMEOUT_COMMAND} cat {shlex.quote(os_release_path)}; "
            "else printf '%s\\n' '__NO_OS_RELEASE__'; fi"
        )
    else:
        mount_options = _READ_ONLY_MOUNT_OPTIONS[filesystem]
        mount_dir = f"/tmp/mtv-root-inspection-{uuid4().hex}"
        quoted_mount_dir = shlex.quote(mount_dir)
        # `mountpoint -q`'s exit code for "not mounted" differs across util-linux versions
        # (e.g. 1 on this guest's RHEL 8), so any non-zero result means there is simply
        # nothing to unmount here rather than a cleanup failure.
        cleanup_script = (
            "cleanup_status=0; "
            f'if {_GUEST_CLEANUP_COMMAND_TIMEOUT} mountpoint -q "$1"; then '
            f'{_GUEST_MOUNT_CLEANUP_TIMEOUT_COMMAND} umount "$1" || cleanup_status=$?; '
            "fi; "
            f'if [ -d "$1" ]; then {_GUEST_CLEANUP_COMMAND_TIMEOUT} rmdir "$1" || cleanup_status=$?; fi; '
            'exit "$cleanup_status"'
        )
        cleanup_command = f"/bin/bash -c {shlex.quote(cleanup_script)} mtv-root-cleanup {quoted_mount_dir}"
        inspection_script = (
            "set -e; "
            f'mount -o {mount_options} {shlex.quote(device_path)} "$1"; '
            'if [ -r "$1/etc/os-release" ]; then '
            'cat "$1/etc/os-release"; '
            "else echo __NO_OS_RELEASE__; fi"
        )
        command = (
            "set -eu; "
            f"mount_dir={quoted_mount_dir}; "
            'mkdir -m 700 "$mount_dir"; '
            f"cleanup() {{ {cleanup_command}; }}; "
            "trap cleanup EXIT; "
            f"{_GUEST_OS_READ_TIMEOUT_COMMAND} /bin/bash -c {shlex.quote(inspection_script)} "
            f"mtv-root-inspection {quoted_mount_dir}"
        )
        timeout_cleanup_command = cleanup_command

    output = run_command_in_vmware_guest(
        content=source_provider.content,
        vm=vm,
        auth=auth,
        command=command,
        vcenter_host=vcenter_host,
        timeout_cleanup_command=timeout_cleanup_command,
    ).strip()
    return None if output == "__NO_OS_RELEASE__" else parse_linux_os_release(output)


def _find_alternate_linux_os_roots(
    source_provider: VMWareProvider,
    vm: vim.VirtualMachine,
    auth: vim.vm.guest.GuestAuthentication,
    vcenter_host: str,
    active_root_device: str,
    block_devices: list[LinuxBlockDevice],
) -> tuple[bool, list[LinuxOSRoot]]:
    """Find OS installations in supported partitions other than the active root.

    Args:
        source_provider: Connected vSphere provider.
        vm: Source VM inspected through Guest Operations.
        auth: Guest Operations credentials.
        vcenter_host: vCenter hostname used by Guest Operations file transfer.
        active_root_device: Partition currently mounted as the guest root.
        block_devices: Parsed records from lsblk's pairs output.

    Returns:
        Whether lsblk contains the active root partition and any other OS roots found.

    Raises:
        ValueError: If the block-device inventory or an OS identity is malformed.
        GuestCommandError: If a guest command or partition mount fails.
        TimeoutExpiredError: If a guest command does not complete within its timeout.
        vim.fault.GuestOperationsFault: If VMware Guest Operations fails.
        urllib.error.URLError: If Guest Operations output cannot be retrieved.
    """
    active_root_found = False
    alternate_roots: list[LinuxOSRoot] = []
    for device in block_devices:
        device_path = device["path"]
        filesystem = device["fstype"]
        if device.get("type") != "part":
            continue
        if device_path == active_root_device:
            active_root_found = filesystem in _SUPPORTED_ROOT_FILESYSTEMS
            continue
        if filesystem not in _SUPPORTED_ROOT_FILESYSTEMS:
            continue

        mountpoint = device["mountpoint"]
        os_identity = _read_linux_os_release_from_partition(
            source_provider=source_provider,
            vm=vm,
            auth=auth,
            vcenter_host=vcenter_host,
            device_path=device_path,
            filesystem=filesystem,
            mountpoint=mountpoint,
        )
        if os_identity is None:
            continue
        alternate_roots.append({
            "root_device": device_path,
            "os_id": os_identity["os_id"],
            "version_id": os_identity["version_id"],
        })

    return active_root_found, alternate_roots


def _select_alternate_linux_os_root(
    vm_name: str,
    active_root_device: str,
    active_os: dict[str, str],
    active_root_found: bool,
    alternate_roots: list[LinuxOSRoot],
) -> LinuxOSRoot:
    """Validate that the guest has exactly one distinct supported alternate OS root.

    Args:
        vm_name: Source VM name for diagnostics.
        active_root_device: Partition currently mounted as the guest root.
        active_os: Distribution identity read from the active root.
        active_root_found: Whether lsblk identified the active root partition.
        alternate_roots: OS roots discovered on other partitions.

    Returns:
        The sole alternate OS root.

    Raises:
        ValueError: If the active root is missing or the alternate root is
            ambiguous, not distinct, or unsupported by Forklift's disk mapping.
    """
    if not active_root_found:
        raise ValueError(
            f"Active root partition {active_root_device} was not found as a supported filesystem in lsblk output "
            f"for VM {vm_name}"
        )
    if len(alternate_roots) != 1:
        raise ValueError(
            f"Expected one alternate Linux root on VM {vm_name}, found {len(alternate_roots)}: "
            f"{[root['root_device'] for root in alternate_roots]}"
        )

    alternate_root = alternate_roots[0]
    if (alternate_root["os_id"], alternate_root["version_id"]) == (
        active_os["os_id"],
        active_os["version_id"],
    ):
        raise ValueError(
            f"Active and alternate roots on VM {vm_name} report the same OS identity "
            f"{active_os['os_id']} {active_os['version_id']}"
        )
    if not _SUPPORTED_ROOT_DEVICE.fullmatch(alternate_root["root_device"]):
        raise ValueError(
            f"Unsupported alternate root device {alternate_root['root_device']!r} for VM {vm_name}; "
            "expected a partition path such as /dev/sdb2"
        )
    return alternate_root


def find_alternate_linux_os_root(
    source_provider: VMWareProvider,
    vm: vim.VirtualMachine,
    source_provider_data: dict[str, Any],
) -> LinuxOSRoot:
    """Find the single Linux installation on a non-booted partition via Guest Operations.

    Other supported partitions are inspected read-only, with filesystem journal
    recovery disabled. The selected partition must contain a distinct OS identity.

    Args:
        source_provider: Connected vSphere provider for the source VM.
        vm: Powered-on source VM with VMware Tools running.
        source_provider_data: Provider configuration containing Linux guest credentials.

    Returns:
        The alternate root partition and its Linux distribution identity.

    Raises:
        ValueError: If credentials, guest state, root user, disk layout, or the
            two distinct Linux installations required by this scenario are missing.
        GuestCommandError: If a command inside the guest fails.
        TimeoutExpiredError: If a guest command does not complete within its timeout.
        vim.fault.GuestOperationsFault: If VMware Guest Operations fails.
        urllib.error.URLError: If Guest Operations output cannot be retrieved.
    """
    if getattr(vm.guest, "guestFamily", None) != "linuxGuest":
        raise ValueError(f"Root OS discovery requires a Linux guest; VM {vm.name} is not Linux")

    try:
        guest_username = source_provider_data["guest_vm_linux_user"]
        guest_password = source_provider_data["guest_vm_linux_password"]
    except KeyError as e:
        raise ValueError(
            f"Linux VM credentials not found in provider config: {e}. "
            "Required: guest_vm_linux_user, guest_vm_linux_password"
        ) from e

    vcenter_host = source_provider.host
    if vcenter_host is None:
        raise ValueError(f"vCenter host not available for provider used by VM {vm.name}")

    auth = vim.vm.guest.NamePasswordAuthentication(
        username=guest_username,
        password=guest_password,
        interactiveSession=False,
    )
    active_root_device, active_os, block_devices = _get_linux_root_inventory(
        source_provider=source_provider,
        vm=vm,
        auth=auth,
        vcenter_host=vcenter_host,
    )
    active_root_found, alternate_roots = _find_alternate_linux_os_roots(
        source_provider=source_provider,
        vm=vm,
        auth=auth,
        vcenter_host=vcenter_host,
        active_root_device=active_root_device,
        block_devices=block_devices,
    )

    alternate_root = _select_alternate_linux_os_root(
        vm_name=vm.name,
        active_root_device=active_root_device,
        active_os=active_os,
        active_root_found=active_root_found,
        alternate_roots=alternate_roots,
    )

    LOGGER.info(
        f"VM {vm.name}: selected alternate root {alternate_root['root_device']} "
        f"({alternate_root['os_id']} {alternate_root['version_id']}); "
        f"active root is {active_root_device} ({active_os['os_id']} {active_os['version_id']})"
    )
    return alternate_root


def run_command_in_vmware_guest(
    content: vim.ServiceInstanceContent,
    vm: vim.VirtualMachine,
    auth: vim.vm.guest.GuestAuthentication,
    command: str,
    vcenter_host: str,
    timeout: int = 30,
    timeout_cleanup_command: str | None = None,
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
        timeout_cleanup_command (str | None): Optional guest command to run after
            terminating the process when it exceeds the timeout.

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
        if guest_family == "linuxGuest":
            # A trailing "> file" on a ";"-separated command list only redirects the
            # last command, silently dropping output from every earlier one. Wrap the
            # whole command in a brace group so the redirect applies to all of it.
            redirect = f"{{ {command}\n}} > {output_file} 2>&1"
            arguments = f"-c {shlex.quote(redirect)}"
        else:
            redirect = f"{command} > {output_file} 2>&1"
            arguments = f'/c "{redirect}"'

        pid = pm.StartProgramInGuest(
            vm=vm,
            auth=auth,
            spec=vim.vm.guest.ProcessManager.ProgramSpec(
                programPath=program_path,
                arguments=arguments,
            ),
        )

        try:
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
        except TimeoutExpiredError as timeout_error:
            try:
                pm.TerminateProcessInGuest(vm=vm, auth=auth, pid=pid)
            except vim.fault.GuestProcessNotFound:
                pass
            if timeout_cleanup_command:
                try:
                    run_command_in_vmware_guest(
                        content=content,
                        vm=vm,
                        auth=auth,
                        command=timeout_cleanup_command,
                        vcenter_host=vcenter_host,
                        timeout=_GUEST_COMMAND_CLEANUP_TIMEOUT,
                    )
                except (
                    GuestCommandError,
                    TimeoutExpiredError,
                    vim.fault.GuestOperationsFault,
                    urllib.error.URLError,
                ) as cleanup_error:
                    raise timeout_error from cleanup_error
            raise

        transfer_info = fm.InitiateFileTransferFromGuest(vm=vm, auth=auth, guestFilePath=output_file)
        url = transfer_info.url
        if "://*" in url:
            url = url.replace("://*", f"://{vcenter_host}")

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(url, context=ctx, timeout=_FILE_TRANSFER_TIMEOUT) as resp:
            output = resp.read().decode("utf-8")

        if process_info.exitCode != 0:
            raise GuestCommandError(
                f"Guest process {pid} on VM {vm.name} exited with code {process_info.exitCode}. Output:\n{output}"
            )

        return output
    finally:
        try:
            fm.DeleteFileInGuest(vm=vm, auth=auth, filePath=output_file)
        except Exception:
            LOGGER.debug(f"Failed to clean up temp file {output_file} in guest VM {vm.name}", exc_info=True)


def _parse_nmcli_ip_data(output: str) -> dict[str, tuple[bool, list[str]]]:
    """Parse nmcli IP data output into per-IP origin and per-connection DNS mappings.

    Args:
        output (str): Raw output from nmcli command, with lines in 'IP|method|dns' format
            (dns is a comma-separated list, possibly empty)

    Returns:
        dict[str, tuple[bool, list[str]]]: Mapping of IP address to (static IP flag,
            DNS servers configured on the IP's NM connection profile)
    """
    ip_data: dict[str, tuple[bool, list[str]]] = {}
    for line in output.strip().splitlines():
        parts = line.split("|", 2)
        if len(parts) != 3:
            continue
        ip_addr, method, dns = (part.strip() for part in parts)
        ip_data[ip_addr] = (method == "manual", [server for server in dns.split(",") if server])
    return ip_data


def _apply_ip_origins_to_vm_details(
    vm_details: dict[str, Any],
    ip_data: dict[str, tuple[bool, list[str]]],
    vm_name: str,
) -> None:
    """Set is_static_ip, ip_origin and dns_servers on IPs with an unknown origin, in-place.

    Origins use the vSphere tools vocabulary ("manual"/"dhcp"); DNS servers come
    from the IP's NM connection profile.

    Args:
        vm_details (dict[str, Any]): VM details dict with network_interfaces data.
        ip_data (dict[str, tuple[bool, list[str]]]): IP address mapped to its
            static flag and NM connection profile DNS servers.
        vm_name (str): VM name for logging.
    """
    for nic in vm_details.get("network_interfaces", []):
        for ip_config in nic.get("ip_addresses", []):
            if ip_config.get("is_static_ip") is not None:
                continue
            ip_addr = ip_config.get("ip_address", "")
            if ip_addr in ip_data:
                is_static_ip, dns_servers = ip_data[ip_addr]
                ip_config["is_static_ip"] = is_static_ip
                ip_config["ip_origin"] = "manual" if is_static_ip else "dhcp"
                ip_config["dns_servers"] = dns_servers
                LOGGER.info(
                    f"VM {vm_name}: IP {ip_addr} origin={ip_config['ip_origin']} "
                    f"dns={dns_servers} (via Guest Operations)"
                )


def detect_vmware_ip_origins_via_guest_ops(
    source_provider: VMWareProvider,
    vm: vim.VirtualMachine,
    source_provider_data: dict[str, Any],
    vm_details: dict[str, Any],
) -> None:
    """Detect IP assignment method and per-NIC DNS for Linux guests via VMware Guest Operations API.

    Workaround for open-vm-tools not reporting IP origin on Linux guests
    (https://github.com/vmware/open-vm-tools/issues/694).
    Runs nmcli inside the guest via vCenter Guest Operations (no SSH needed).

    Updates vm_details in-place with detected IP origins and per-connection
    DNS servers.

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
        'dns=$(nmcli -g ipv4.dns connection show "$uuid" 2>/dev/null); '
        'ip -4 -o addr show "$dev" 2>/dev/null | while read -r _ _ _ cidr _; do '
        'printf "%s|%s|%s\\n" "${cidr%%/*}" "$method" "$dns"; '
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

    ip_data = _parse_nmcli_ip_data(output)
    if not ip_data:
        LOGGER.warning(f"No IP origin data detected via Guest Operations for VM {vm.name}")
        return

    _apply_ip_origins_to_vm_details(vm_details=vm_details, ip_data=ip_data, vm_name=vm.name)


def detect_guest_nic_names(
    source_provider: VMWareProvider,
    vm: vim.VirtualMachine,
    source_provider_data: dict[str, Any],
    vm_details: dict[str, Any],
) -> None:
    """Detect guest OS NIC names via VMware Guest Operations API.

    Runs 'ip -j link show' inside the guest to collect NIC name and MAC address pairs,
    then stores the guest NIC name on matching network interfaces in vm_details.

    Note: Only supports Linux guest operating systems. The caller must ensure this
    is only invoked for VMs running a Linux guest OS.

    Args:
        source_provider: VMware provider instance with content and host attributes
        vm: VMware VM object (must be powered on with VMware Tools running)
        source_provider_data: Provider config containing guest credentials
        vm_details: VM details dict from vm_dict(), updated in-place

    Raises:
        ValueError: If guest credentials are not found in provider config
        GuestCommandError: If the guest command fails
        json.JSONDecodeError: If the command output is not valid JSON
    """
    try:
        guest_username = source_provider_data["guest_vm_linux_user"]
        guest_password = source_provider_data["guest_vm_linux_password"]
    except KeyError as e:
        raise ValueError(
            f"Linux VM credentials not found in provider config: {e}. "
            "Required: guest_vm_linux_user, guest_vm_linux_password"
        ) from e

    LOGGER.info(f"Detecting guest NIC names via Guest Operations for VM {vm.name}")

    auth = vim.vm.guest.NamePasswordAuthentication(
        username=guest_username, password=guest_password, interactiveSession=False
    )

    vcenter_host = source_provider.host
    if vcenter_host is None:
        raise ValueError(f"vCenter host not available for provider used by VM {vm.name}")

    output = run_command_in_vmware_guest(
        content=source_provider.content,
        vm=vm,
        auth=auth,
        command="ip -j link show",
        vcenter_host=vcenter_host,
    )

    interfaces = json.loads(output)
    for iface in interfaces:
        nic_name = iface.get("ifname", "")
        mac = iface.get("address", "").lower()
        if not mac or nic_name == "lo":
            continue

        for nic in vm_details.get("network_interfaces", []):
            if nic.get("macAddress", "").lower() == mac:
                nic["guest_nic_name"] = nic_name
                LOGGER.info(f"VM {vm.name}: NIC {nic_name} matched to MAC {mac}")
                break
        else:
            LOGGER.warning(f"VM {vm.name}: NIC {nic_name} not found in vm_details")


def collect_linux_guest_static_ip_metadata(
    source_provider: VMWareProvider,
    vm: vim.VirtualMachine,
    source_provider_data: dict[str, Any],
    vm_details: dict[str, Any],
    preserve_static_ips: bool,
) -> None:
    """Detect IP origins and, when preserving static IPs, NIC names for a Linux vSphere guest.

    VMware's open-vm-tools does not report IP origin for Linux guests
    (https://github.com/vmware/open-vm-tools/issues/694), so origins are detected through Guest
    Operations. Windows guests (``win_os``) are skipped — their origins are reported natively.
    When ``preserve_static_ips`` is enabled, IP-origin detection is mandatory (a failure raises)
    and guest NIC names are collected too. Updates ``vm_details`` in place.

    Both the cloning and ESXi skip-clone preparation paths call this so static-IP preservation
    validation sees identical source metadata regardless of how the VM was prepared.

    Args:
        source_provider (VMWareProvider): VMware provider instance.
        vm (vim.VirtualMachine): VMware VM object (powered on, VMware Tools running).
        source_provider_data (dict[str, Any]): Provider config containing guest credentials.
        vm_details (dict[str, Any]): VM details dict from vm_dict(), updated in place.
        preserve_static_ips (bool): Whether the plan preserves static IPs; makes IP-origin
            detection mandatory and triggers NIC-name collection.

    Raises:
        ValueError: If IP-origin detection fails while preserve_static_ips is enabled, or guest
            credentials are missing.
    """
    if vm_details.get("win_os"):
        return

    try:
        detect_vmware_ip_origins_via_guest_ops(
            source_provider=source_provider,
            vm=vm,
            source_provider_data=source_provider_data,
            vm_details=vm_details,
        )
    except ValueError:
        raise
    except (GuestCommandError, vim.fault.VimFault, TimeoutExpiredError, OSError) as e:
        if preserve_static_ips:
            raise ValueError(
                f"Failed to detect IP origins via Guest Operations for VM {vm_details['name']}: {e}. "
                "IP origin detection is required when preserve_static_ips is enabled."
            ) from e
        LOGGER.warning(
            f"Failed to detect IP origins via Guest Operations for VM {vm_details['name']}: {e}. "
            "Static IP verification may not work for this VM."
        )

    if preserve_static_ips:
        detect_guest_nic_names(
            source_provider=source_provider,
            vm=vm,
            source_provider_data=source_provider_data,
            vm_details=vm_details,
        )


def create_data_integrity_marker(
    source_provider: VMWareProvider,
    vm: vim.VirtualMachine,
    source_provider_data: dict[str, Any],
    marker_content: str,
) -> None:
    """Create a test directory and marker file inside a VMware guest for post-migration data integrity validation.

    Writes a known marker string to a file on the guest filesystem so that
    after migration the same file can be read back to confirm disk data was preserved.

    Uses echo piped to tee so that run_command_in_vmware_guest()'s outer stdout
    redirect (> output 2>&1) does not clobber the marker file. The marker content
    must contain only shell-safe characters (alphanumeric, hyphens, underscores).

    Args:
        source_provider (VMWareProvider): VMware provider instance.
        vm (vim.VirtualMachine): VMware VM object (must be powered on with VMware Tools running).
        source_provider_data (dict[str, Any]): Provider config containing guest credentials.
        marker_content (str): String to write into the marker file.

    Raises:
        ValueError: If guest credentials are not found or marker content has unsafe characters.
        GuestCommandError: If the guest command exits with a non-zero exit code.
    """
    if not all(c.isalnum() or c in "-_." for c in marker_content):
        raise ValueError(f"Marker content contains shell-unsafe characters: {marker_content!r}")

    try:
        guest_username = source_provider_data["guest_vm_linux_user"]
        guest_password = source_provider_data["guest_vm_linux_password"]
    except KeyError as e:
        raise ValueError(
            f"Linux VM credentials not found in provider config: {e}. "
            "Required: guest_vm_linux_user, guest_vm_linux_password"
        ) from e

    guest_family = getattr(vm.guest, "guestFamily", None)
    if guest_family != "linuxGuest":
        raise ValueError(
            f"Data integrity marker requires a Linux guest, but VM {vm.name} has guestFamily={guest_family!r}"
        )

    auth = vim.vm.guest.NamePasswordAuthentication(
        username=guest_username, password=guest_password, interactiveSession=False
    )

    vcenter_host = source_provider.host
    if vcenter_host is None:
        raise ValueError(f"vCenter host not available for provider used by VM {vm.name}")

    command = f"mkdir -p {DATA_INTEGRITY_DIR} && echo {marker_content} | tee {DATA_INTEGRITY_FILE}"

    LOGGER.info(f"Creating data integrity marker on VM {vm.name} at {DATA_INTEGRITY_FILE}")
    run_command_in_vmware_guest(
        content=source_provider.content,
        vm=vm,
        auth=auth,
        command=command,
        vcenter_host=vcenter_host,
    )

    readback = run_command_in_vmware_guest(
        content=source_provider.content,
        vm=vm,
        auth=auth,
        command=f"cat {DATA_INTEGRITY_FILE}",
        vcenter_host=vcenter_host,
    )
    LOGGER.info(f"Marker read-back on source VM {vm.name}: {readback.strip()!r}")

    if readback.strip() != marker_content:
        raise GuestCommandError(
            f"Marker verification failed on source VM {vm.name}: expected {marker_content!r}, got {readback.strip()!r}"
        )
