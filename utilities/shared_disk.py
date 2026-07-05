"""Shared disk migration verification utilities.

Provides functions for verifying shared disk accessibility between VMs
after migration.
"""

from __future__ import annotations

import base64
import re
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ocp_resources.virtual_machine import VirtualMachine
from paramiko.ssh_exception import AuthenticationException, ChannelException, NoValidConnectionsError, SSHException
from pyVmomi import vim
from simple_logger.logger import get_logger

from timeout_sampler import TimeoutExpiredError, TimeoutSampler

from exceptions.exceptions import GuestCommandError, SSHConnectionSetupError
from utilities.naming import resolve_destination_vm_name
from utilities.post_migration import get_ssh_credentials_from_provider_config
from utilities.ssh_utils import SSHConnectionManager, VMSSHConnection, run_cmd_in_vm
from utilities.vmware_guest_operations import run_command_in_vmware_guest

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

    from libs.providers.vmware import VMWareProvider

LOGGER = get_logger(name=__name__)

_VMI_VOLUME_STATUS_TIMEOUT = 300
_VMI_VOLUME_STATUS_POLL_INTERVAL = 5

_SHARED_LABEL_PREFIX = "SHARED"

_WIN_LABEL_PS_TEMPLATE = """\
$ErrorActionPreference = 'Stop'
$wmiDisk = Get-CimInstance Win32_DiskDrive | Where-Object {{ $_.SerialNumber -eq '{serial}' }}
if (-not $wmiDisk) {{ throw 'No disk with serial {serial}' }}
$n = $wmiDisk.Index
Set-Disk -Number $n -IsOffline $false -ErrorAction SilentlyContinue
Set-Disk -Number $n -IsReadOnly $false -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
$vol = Get-Partition -DiskNumber $n | Get-Volume | Where-Object {{ $_.FileSystemType -eq 'NTFS' -and $_.DriveLetter }}
if (-not $vol) {{ throw ('No NTFS volume with drive letter on disk ' + $n) }}
$vol | ForEach-Object {{ Set-Volume -DriveLetter $_.DriveLetter -NewFileSystemLabel '{label}' }}
$check = Get-Volume -FileSystemLabel '{label}' -ErrorAction SilentlyContinue
if (-not $check) {{ throw 'Label verification failed' }}
Write-Output ('Labeled disk ' + $n + ' drive ' + $check.DriveLetter + ': as {label}')
"""

_WIN_LABEL_GUEST_OPS_TIMEOUT = 60
_WIN_VOLUME_DISCOVERY_TIMEOUT = 60
_WIN_VOLUME_DISCOVERY_POLL_INTERVAL = 5
_WIN_VERIFICATION_RETRY_TIMEOUT = 300
_WIN_VERIFICATION_RETRY_INTERVAL = 15
_GUEST_TOOLS_READY_TIMEOUT = 300

_HEX_SERIAL_RE = re.compile(r"[a-fA-F0-9]+")
_SAFE_LABEL_RE = re.compile(r"[a-zA-Z0-9_-]+")

_MARKER_VM1_FILENAME = "test-vm1.txt"
_MARKER_VM2_FILENAME = "test-vm2.txt"
_MARKER_VM1_CONTENT = "Data from VM1"
_MARKER_VM2_CONTENT = "Data from VM2"


def _mount_shared_partition(ssh_conn: VMSSHConnection, partition: str, mount_point: str, vm_label: str) -> None:
    """Mount a shared disk partition on a VM.

    Args:
        ssh_conn (VMSSHConnection): Active SSH connection to the VM.
        partition (str): Device partition path (e.g., "/dev/vdc1").
        mount_point (str): Mount target directory.
        vm_label (str): Label for log messages (e.g., "VM1").

    Raises:
        GuestCommandError: If mkdir or mount command fails.
    """
    run_cmd_in_vm(ssh_conn, ["sudo", "mkdir", "-p", mount_point], f"{vm_label} mkdir")
    run_cmd_in_vm(ssh_conn, ["sudo", "mount", partition, mount_point], f"{vm_label} mount")


def _umount_shared_partition(ssh_conn: VMSSHConnection, mount_point: str, vm_label: str) -> None:
    """Unmount a shared disk partition on a VM.

    Args:
        ssh_conn (VMSSHConnection): Active SSH connection to the VM.
        mount_point (str): Mount point to unmount.
        vm_label (str): Label for log messages (e.g., "VM1").

    Raises:
        GuestCommandError: If umount command fails.
    """
    run_cmd_in_vm(ssh_conn, ["sudo", "umount", mount_point], f"{vm_label} umount")


def _write_marker(ssh_conn: VMSSHConnection, file_path: str, content: str, vm_label: str) -> None:
    """Write a marker file and sync to disk.

    Args:
        ssh_conn (VMSSHConnection): Active SSH connection to the VM.
        file_path (str): Absolute path for the marker file.
        content (str): Text content to write.
        vm_label (str): Label for log messages (e.g., "VM1").

    Raises:
        GuestCommandError: If write or sync command fails.
    """
    run_cmd_in_vm(
        ssh_conn,
        ["sh", "-c", f"echo {shlex.quote(content)} | sudo tee {shlex.quote(file_path)} > /dev/null"],
        f"{vm_label} write test data",
    )
    run_cmd_in_vm(ssh_conn, ["sudo", "sync"], f"{vm_label} sync")


def _get_pvc_device_targets(
    ocp_admin_client: DynamicClient,
    target_namespace: str,
    vm_name: str,
) -> dict[str, str]:
    """Map PVC claim names to their runtime device targets from VMI status.

    Uses ``volumeStatus[].target`` which contains the actual device name
    assigned by KubeVirt at runtime (e.g. ``vda``), eliminating any
    dependency on volume ordering or index arithmetic.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        target_namespace (str): Namespace where the migrated VM lives.
        vm_name (str): Destination VM name (already sanitized for Kubernetes).

    Returns:
        dict[str, str]: Mapping of PVC claim name to device path,
            e.g. ``{"pvc-boot": "/dev/vda", "pvc-shared": "/dev/vdc"}``.

    Raises:
        ValueError: If PVC device targets are not populated within the
            timeout (e.g. VMI not running or volumeStatus not yet available).
    """
    cnv_vm = VirtualMachine(
        client=ocp_admin_client,
        name=vm_name,
        namespace=target_namespace,
        ensure_exists=True,
    )
    pvc_devices: dict[str, str] = {}
    sample = None
    try:
        for sample in TimeoutSampler(
            wait_timeout=_VMI_VOLUME_STATUS_TIMEOUT,
            sleep=_VMI_VOLUME_STATUS_POLL_INTERVAL,
            func=lambda: cnv_vm.vmi.instance if cnv_vm.vmi else None,
        ):
            if not sample:
                continue
            volume_status = getattr(sample.status, "volumeStatus", None)
            if not volume_status:
                continue
            pvc_devices.clear()
            for vol_status in volume_status:
                pvc_info = getattr(vol_status, "persistentVolumeClaimInfo", None)
                if not pvc_info:
                    continue
                if not vol_status.target:
                    pvc_devices.clear()
                    break
                pvc_devices[pvc_info.claimName] = f"/dev/{vol_status.target}"
            if pvc_devices:
                break
    except TimeoutExpiredError as exc:
        phase = getattr(sample.status, "phase", "unknown") if sample else "no-sample"
        raise ValueError(
            f"VM '{vm_name}' in '{target_namespace}' VMI has no PVC device targets "
            f"after {_VMI_VOLUME_STATUS_TIMEOUT}s (phase: {phase})"
        ) from exc
    LOGGER.debug(f"PVC device targets for VM '{vm_name}': {pvc_devices}")
    return pvc_devices


def _get_shared_disk_devices(
    ocp_admin_client: DynamicClient,
    target_namespace: str,
    vm1_dest_name: str,
    vm2_dest_name: str,
) -> dict[str, str]:
    """Determine per-VM shared disk device paths from destination PVC references.

    Finds the PVC referenced by both destination VMs (the shared disk) and
    returns each VM's device path using the runtime device target from
    ``VMI status.volumeStatus``. Handles VMs with different disk layouts
    correctly.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        target_namespace (str): Namespace where migrated VMs live.
        vm1_dest_name (str): First destination VM name.
        vm2_dest_name (str): Second destination VM name.

    Returns:
        dict[str, str]: Mapping of destination VM name to device path,
            e.g. ``{"vm1-name": "/dev/vdc", "vm2-name": "/dev/vdb"}``.

    Raises:
        ValueError: If no shared PVC found between the two VMs.
    """
    vm1_pvc_devices = _get_pvc_device_targets(ocp_admin_client, target_namespace, vm1_dest_name)
    vm2_pvc_devices = _get_pvc_device_targets(ocp_admin_client, target_namespace, vm2_dest_name)

    shared_pvcs = set(vm1_pvc_devices) & set(vm2_pvc_devices)
    if not shared_pvcs:
        raise ValueError(
            f"No shared PVC between '{vm1_dest_name}' and '{vm2_dest_name}'. "
            f"VM1 PVCs: {list(vm1_pvc_devices)}, VM2 PVCs: {list(vm2_pvc_devices)}"
        )
    if len(shared_pvcs) > 1:
        raise ValueError(
            f"Multiple shared PVCs between '{vm1_dest_name}' and '{vm2_dest_name}': {shared_pvcs}. "
            "Only single shared disk is supported."
        )

    shared_pvc = shared_pvcs.pop()
    vm1_device = vm1_pvc_devices[shared_pvc]
    vm2_device = vm2_pvc_devices[shared_pvc]
    LOGGER.info(f"Shared PVC '{shared_pvc}': '{vm1_dest_name}' -> {vm1_device}, '{vm2_dest_name}' -> {vm2_device}")
    return {vm1_dest_name: vm1_device, vm2_dest_name: vm2_device}


@dataclass
class _SharedDiskContext:
    """Common context extracted by _prepare_shared_disk_verification."""

    vm1_name: str
    vm2_name: str
    vm1_dest_name: str
    vm2_dest_name: str
    ssh_vm1: VMSSHConnection
    ssh_vm2: VMSSHConnection
    shared_devices: dict[str, str]


def _prepare_shared_disk_verification(
    prepared_plan: dict[str, Any],
    vm_ssh_connections: SSHConnectionManager,
    source_provider_data: dict[str, Any],
    ocp_admin_client: DynamicClient,
) -> _SharedDiskContext:
    """Extract common setup for shared disk verification (Linux and Windows).

    Both verify functions share the same preamble: extract VM configs,
    resolve destination names, validate shared PVC exists, get SSH
    credentials, and create SSH connections.

    Args:
        prepared_plan (dict[str, Any]): Plan config with virtual_machines, source_vms_data, and _vm_target_namespace.
        vm_ssh_connections (SSHConnectionManager): SSH connection manager.
        source_provider_data (dict[str, Any]): Provider configuration from .providers.json.
        ocp_admin_client (DynamicClient): OpenShift admin client for destination VM lookup.

    Returns:
        _SharedDiskContext: Common context for shared disk verification.

    Raises:
        ValueError: If no shared PVC (or more than one) is found between the two VMs.
    """
    vm1_config = prepared_plan["virtual_machines"][0]
    vm2_config = prepared_plan["virtual_machines"][1]
    vm1_name = vm1_config["name"]
    vm2_name = vm2_config["name"]
    vm_namespace = prepared_plan["_vm_target_namespace"]
    vm1_dest_name = resolve_destination_vm_name(vm1_config)
    vm2_dest_name = resolve_destination_vm_name(vm2_config)
    shared_devices = _get_shared_disk_devices(ocp_admin_client, vm_namespace, vm1_dest_name, vm2_dest_name)

    vm1_info = prepared_plan["source_vms_data"][vm1_name]
    vm2_info = prepared_plan["source_vms_data"][vm2_name]
    vm1_user, vm1_pass = get_ssh_credentials_from_provider_config(source_provider_data, vm1_info)
    vm2_user, vm2_pass = get_ssh_credentials_from_provider_config(source_provider_data, vm2_info)
    ssh_vm1 = vm_ssh_connections.create(vm_name=vm1_name, username=vm1_user, password=vm1_pass)
    ssh_vm2 = vm_ssh_connections.create(vm_name=vm2_name, username=vm2_user, password=vm2_pass)

    return _SharedDiskContext(
        vm1_name=vm1_name,
        vm2_name=vm2_name,
        vm1_dest_name=vm1_dest_name,
        vm2_dest_name=vm2_dest_name,
        ssh_vm1=ssh_vm1,
        ssh_vm2=ssh_vm2,
        shared_devices=shared_devices,
    )


def verify_shared_disk_data(
    prepared_plan: dict[str, Any],
    vm_ssh_connections: SSHConnectionManager,
    source_provider_data: dict[str, Any],
    ocp_admin_client: DynamicClient,
) -> None:
    """Verify shared disk is accessible from both VMs by writing and reading data.

    The shared disk must already be formatted with a filesystem and unmounted.
    (MTV-2200 limitation: virt-v2v cannot update fstab for shared disks.)

    Flow:
    1. VM1: mount shared disk, write test data, sync, unmount
    2. VM2: mount shared disk, read VM1's data, write own data, sync, unmount
    3. VM1: flush block device cache, remount, read VM2's data, unmount

    Args:
        prepared_plan (dict[str, Any]): Plan config with virtual_machines, source_vms_data, and _vm_target_namespace.
        vm_ssh_connections (SSHConnectionManager): SSH connection manager.
        source_provider_data (dict[str, Any]): Provider configuration from .providers.json.
        ocp_admin_client (DynamicClient): OpenShift admin client for destination VM lookup.

    Raises:
        GuestCommandError: If SSH commands fail.
        AssertionError: If shared-disk marker content does not match.
    """
    ctx = _prepare_shared_disk_verification(prepared_plan, vm_ssh_connections, source_provider_data, ocp_admin_client)
    vm1_device = ctx.shared_devices[ctx.vm1_dest_name]
    vm2_device = ctx.shared_devices[ctx.vm2_dest_name]

    LOGGER.info(f"Verifying shared disk between {ctx.vm1_name} and {ctx.vm2_name}")

    mount_point = "/mnt/shared_disk"
    vm1_partition = f"{vm1_device}1"
    vm2_partition = f"{vm2_device}1"
    test_file_vm1 = f"{mount_point}/test-vm1.txt"
    test_file_vm2 = f"{mount_point}/test-vm2.txt"

    LOGGER.info(f"VM1 ({ctx.vm1_name}): Mounting shared disk {vm1_partition}")
    with ctx.ssh_vm1:
        _mount_shared_partition(ctx.ssh_vm1, vm1_partition, mount_point, "VM1")
        _write_marker(ctx.ssh_vm1, test_file_vm1, "Data from VM1", "VM1")
        _umount_shared_partition(ctx.ssh_vm1, mount_point, "VM1")

        LOGGER.info(f"VM2 ({ctx.vm2_name}): Mounting shared disk {vm2_partition}")
        with ctx.ssh_vm2:
            _mount_shared_partition(ctx.ssh_vm2, vm2_partition, mount_point, "VM2")

            vm2_read_data = run_cmd_in_vm(ctx.ssh_vm2, ["sudo", "cat", test_file_vm1], "VM2 read VM1 data")
            assert "Data from VM1" in vm2_read_data.strip(), f"VM2 cannot read VM1's data: {vm2_read_data}"
            LOGGER.info(f"VM2 ({ctx.vm2_name}): Successfully read VM1's data")

            _write_marker(ctx.ssh_vm2, test_file_vm2, "Data from VM2", "VM2")
            _umount_shared_partition(ctx.ssh_vm2, mount_point, "VM2")

        LOGGER.info(f"VM1 ({ctx.vm1_name}): Verifying bidirectional access")
        # Flush block device buffers to clear stale kernel cache.
        # XFS (non-cluster filesystem) retains metadata in kernel buffer cache.
        # Without this, VM1 won't see VM2's newly written files even after remount.
        run_cmd_in_vm(ctx.ssh_vm1, ["sudo", "blockdev", "--flushbufs", vm1_device], "VM1 flush buffers")
        run_cmd_in_vm(ctx.ssh_vm1, ["sudo", "mount", vm1_partition, mount_point], "VM1 remount")

        vm1_read_data = run_cmd_in_vm(ctx.ssh_vm1, ["sudo", "cat", test_file_vm2], "VM1 read VM2 data")
        assert "Data from VM2" in vm1_read_data.strip(), f"VM1 cannot read VM2's data: {vm1_read_data}"
        LOGGER.info(f"VM1 ({ctx.vm1_name}): Successfully read VM2's data")

        _umount_shared_partition(ctx.ssh_vm1, mount_point, "VM1 final")

    LOGGER.info("Shared disk verification successful - bidirectional access confirmed")


def _find_shared_disk_serial(
    source_provider: VMWareProvider,
    owner_vm: vim.VirtualMachine,
    owner_name: str,
    vm_names: list[str],
) -> str:
    """Find the disk serial of the shared VMDK on the owner VM.

    Detects shared VMDKs by comparing backing files across VMs, then
    extracts the VMware backing UUID (which forklift maps to the Windows
    disk serial via ``Win32_DiskDrive.SerialNumber``).

    Args:
        source_provider (VMWareProvider): VMWare provider instance.
        owner_vm (vim.VirtualMachine): Owner VM pyvmomi object.
        owner_name (str): Owner VM name (for error messages).
        vm_names (list[str]): All VM names in the plan.

    Returns:
        str: Disk serial string (backing UUID without dashes, lowercase).

    Raises:
        ValueError: If no shared VMDK found or backing UUID unavailable.
    """
    shared_vmdks = source_provider.find_shared_vmdk_paths(vm_names)
    if not shared_vmdks:
        raise ValueError(f"No shared VMDKs found between VMs: {vm_names}")
    if len(shared_vmdks) > 1:
        raise ValueError(
            f"Multiple shared VMDKs found between VMs {vm_names}: {list(shared_vmdks)}. "
            "Only single shared disk is supported."
        )

    shared_vmdk_path = next(iter(shared_vmdks))
    backing_uuid = None
    for device in owner_vm.config.hardware.device:
        if not isinstance(device, vim.vm.device.VirtualDisk):
            continue
        if (
            isinstance(device.backing, vim.vm.device.VirtualDisk.FlatVer2BackingInfo)
            and device.backing.fileName == shared_vmdk_path
        ):
            backing_uuid = device.backing.uuid
            break
    if not backing_uuid:
        raise ValueError(f"Cannot find backing UUID for shared VMDK '{shared_vmdk_path}' on VM '{owner_name}'")

    serial = backing_uuid.replace("-", "").lower()
    LOGGER.info(f"Shared disk on '{owner_name}': backing.uuid={backing_uuid}, serial={serial}")
    return serial


def _disable_fast_startup(
    source_provider: VMWareProvider,
    owner_vm: vim.VirtualMachine,
    owner_name: str,
    auth: vim.vm.guest.NamePasswordAuthentication,
    vcenter_host: str,
) -> None:
    """Disable Windows Fast Startup via Guest Ops to prevent hiberfile.sys creation.

    Windows hybrid shutdown writes a hibernation file that causes virt-customize to fail
    during migration (ConversionHasWarnings / CustomizationFailed).

    Note:
        Intentional deviation from no-fallbacks rule — Fast Startup disable is
        a non-critical optimization that does not affect migration correctness.
        Failure is logged and execution continues.

    Args:
        source_provider (VMWareProvider): VMWare provider instance.
        owner_vm (vim.VirtualMachine): PyVmomi VM object for the owner VM.
        owner_name (str): Display name of the owner VM.
        auth (vim.vm.guest.NamePasswordAuthentication): Guest authentication credentials.
        vcenter_host (str): vCenter hostname for Guest Ops API.
    """
    try:
        run_command_in_vmware_guest(
            content=source_provider.content,
            vm=owner_vm,
            auth=auth,
            command="powershell -Command powercfg /h off",
            vcenter_host=vcenter_host,
            timeout=_WIN_LABEL_GUEST_OPS_TIMEOUT,
        )
        LOGGER.info(f"Disabled Fast Startup on '{owner_name}' to ensure clean shutdown")
    except (GuestCommandError, vim.fault.VimFault, TimeoutExpiredError, ConnectionError, OSError) as e:
        # Best-effort: Fast Startup disable is non-critical — migration can succeed
        # without it, but may produce ConversionHasWarnings.
        LOGGER.warning(f"Failed to disable Fast Startup on '{owner_name}' (best-effort): {e}")


def _get_guest_auth(
    source_provider: VMWareProvider,
    owner_vm: vim.VirtualMachine,
    owner_name: str,
    source_provider_data: dict[str, Any],
) -> tuple[vim.vm.guest.NamePasswordAuthentication, str]:
    """Wait for VMware Tools and return guest auth + vCenter host.

    Args:
        source_provider (VMWareProvider): VMWare provider instance.
        owner_vm (vim.VirtualMachine): PyVmomi VM object for the owner VM.
        owner_name (str): Display name of the owner VM.
        source_provider_data (dict[str, Any]): Provider config (uses guest_vm_win_user/password).

    Returns:
        tuple: (NamePasswordAuthentication, vcenter_host).

    Raises:
        ValueError: If Tools not ready, credentials missing, or vCenter host unavailable.
    """
    win_user = source_provider_data.get("guest_vm_win_user")
    win_pass = source_provider_data.get("guest_vm_win_password")
    if not win_user or not win_pass:
        raise ValueError(
            f"Windows Guest Operations requires 'guest_vm_win_user' and "
            f"'guest_vm_win_password' in provider config for '{owner_name}'"
        )

    vcenter_host = source_provider.host
    if vcenter_host is None:
        raise ValueError(f"vCenter host not available for provider used by VM '{owner_name}'")

    if not source_provider.wait_for_vmware_guest_info(owner_vm, timeout=_GUEST_TOOLS_READY_TIMEOUT):
        raise ValueError(f"VMware Tools not available on '{owner_name}' after power-on, cannot label shared disk")

    auth = vim.vm.guest.NamePasswordAuthentication(username=win_user, password=win_pass, interactiveSession=False)

    return auth, vcenter_host


def _execute_guest_label_command(
    source_provider: VMWareProvider,
    owner_vm: vim.VirtualMachine,
    owner_name: str,
    disk_serial: str,
    volume_label: str,
    auth: vim.vm.guest.NamePasswordAuthentication,
    vcenter_host: str,
) -> None:
    """Label the shared disk on the source Windows VM via Guest Ops.

    Args:
        source_provider (VMWareProvider): VMWare provider instance.
        owner_vm (vim.VirtualMachine): PyVmomi VM object for the owner VM.
        owner_name (str): Display name of the owner VM.
        disk_serial (str): Hex disk serial number to match inside the guest.
        volume_label (str): Alphanumeric NTFS volume label to set.
        auth (vim.vm.guest.NamePasswordAuthentication): Guest authentication credentials.
        vcenter_host (str): vCenter hostname.

    Raises:
        ValueError: If disk_serial or volume_label format is invalid.
        GuestCommandError: If the labeling PowerShell command fails.
    """
    disk_serial = disk_serial.strip()
    volume_label = volume_label.strip()
    if not _HEX_SERIAL_RE.fullmatch(disk_serial):
        raise ValueError(f"Invalid disk serial (hex expected): {disk_serial}")
    if not _SAFE_LABEL_RE.fullmatch(volume_label):
        raise ValueError(f"Invalid volume label (alphanumeric expected): {volume_label}")

    ps_script = _WIN_LABEL_PS_TEMPLATE.format(serial=disk_serial, label=volume_label)
    encoded_cmd = base64.b64encode(ps_script.encode("utf-16-le")).decode("ascii")

    output = run_command_in_vmware_guest(
        content=source_provider.content,
        vm=owner_vm,
        auth=auth,
        command=f"powershell -EncodedCommand {encoded_cmd}",
        vcenter_host=vcenter_host,
        timeout=_WIN_LABEL_GUEST_OPS_TIMEOUT,
    )
    LOGGER.info(f"Shared disk labeling on '{owner_name}': {output.strip()}")


def label_shared_disk_on_source_windows(
    source_provider: VMWareProvider,
    prepared_plan: dict[str, Any],
    source_provider_data: dict[str, Any],
    session_uuid: str,
) -> None:
    """Label the shared disk on the source Windows VM before migration.

    Sets a unique NTFS volume label via VMware Guest Operations by matching
    the shared VMDK's backing UUID to the Windows disk serial number.
    The generated label is stored in ``prepared_plan["_shared_disk_label"]``
    for post-migration verification.

    Args:
        source_provider (VMWareProvider): VMWare provider instance.
        prepared_plan (dict[str, Any]): Plan config dict (from prepared_plan fixture).
        source_provider_data (dict[str, Any]): Provider config from .providers.json.
        session_uuid (str): Session-unique identifier (from fixture_store).

    Raises:
        ValueError: If no shared disk found or Windows credentials missing.
        GuestCommandError: If PowerShell commands fail inside the guest.
    """
    volume_label = f"{_SHARED_LABEL_PREFIX}-{session_uuid}"
    prepared_plan["_shared_disk_label"] = volume_label
    LOGGER.info(f"Generated shared disk volume label: '{volume_label}'")
    vm_configs = prepared_plan["virtual_machines"]
    vm_names = [vm["name"] for vm in vm_configs]

    owner_idx = next(
        (idx for idx, vm in enumerate(vm_configs) if vm.get("migrate_shared_disks") is True),
        None,
    )
    if owner_idx is None:
        raise ValueError("No VM with migrate_shared_disks=True found in plan")

    owner_name = vm_names[owner_idx]
    owner_vm = prepared_plan["source_vms_data"][owner_name]["provider_vm_api"]

    disk_serial = _find_shared_disk_serial(source_provider, owner_vm, owner_name, vm_names)

    LOGGER.info(f"Powering on '{owner_name}' for shared disk labeling")
    source_provider.start_vm(owner_vm)
    try:
        auth, vcenter_host = _get_guest_auth(
            source_provider=source_provider,
            owner_vm=owner_vm,
            owner_name=owner_name,
            source_provider_data=source_provider_data,
        )
        _execute_guest_label_command(
            source_provider=source_provider,
            owner_vm=owner_vm,
            owner_name=owner_name,
            disk_serial=disk_serial,
            volume_label=volume_label,
            auth=auth,
            vcenter_host=vcenter_host,
        )
        _disable_fast_startup(
            source_provider=source_provider,
            owner_vm=owner_vm,
            owner_name=owner_name,
            auth=auth,
            vcenter_host=vcenter_host,
        )
    finally:
        LOGGER.info(f"Gracefully shutting down '{owner_name}' after shared disk labeling")
        try:
            source_provider.shutdown_vm_guest(owner_vm)
        except (vim.fault.VimFault, ConnectionError, OSError, TimeoutExpiredError) as e:
            LOGGER.warning(f"Failed to shut down '{owner_name}' after labeling: {e}")


def _win_run_powershell(
    ssh_conn: VMSSHConnection,
    script: str,
    description: str,
) -> str:
    """Execute a PowerShell command on a Windows VM via SSH.

    SSH on Windows lands in CMD by default. This wraps the script
    in ``powershell -Command "..."`` so it is interpreted by PowerShell.

    Args:
        ssh_conn (VMSSHConnection): Active SSH connection to the Windows VM.
        script (str): PowerShell script to execute (single command or semicolon-separated).
        description (str): Human-readable description for logging.

    Returns:
        str: Command stdout.

    Raises:
        GuestCommandError: If the command fails (non-zero return code).
    """
    return run_cmd_in_vm(ssh_conn, ["powershell", "-Command", script], description)


def _win_ensure_shared_volume_online(ssh_conn: VMSSHConnection, vm_label: str, volume_label: str) -> str:
    """Bring offline disks online and return the shared volume drive letter.

    After migration, Windows SAN policy may leave non-boot disks offline.
    This brings all offline disks online, clears read-only flags, then
    locates the shared volume by its filesystem label.

    Args:
        ssh_conn (VMSSHConnection): Active SSH connection to the Windows VM.
        vm_label (str): Label for log messages (e.g., "VM1").
        volume_label (str): NTFS volume label to search for.

    Returns:
        str: Single-character drive letter (e.g., "E").

    Raises:
        GuestCommandError: If shared volume not found after bringing disks online.
    """
    # Intentional best-effort (no-fallbacks exception): piped Set-Disk fails on some vSphere
    # versions due to a Windows SSH console buffer bug. _win_refresh_shared_disk uses targeted
    # Set-Disk -Number <N> as the reliable path; these are just optimistic first attempts.
    try:
        _win_run_powershell(
            ssh_conn,
            "Get-Disk | Where-Object {$_.OperationalStatus -eq 'Offline'} | Set-Disk -IsOffline $false | Out-Null",
            f"{vm_label} bring offline disks online",
        )
    except GuestCommandError as e:
        LOGGER.warning(f"{vm_label}: Set-Disk -IsOffline failed (best-effort): {e}")
    try:
        _win_run_powershell(
            ssh_conn,
            "Get-Disk | Where-Object {$_.IsReadOnly -eq $true -and $_.Number -ne 0} "
            "| Set-Disk -IsReadOnly $false | Out-Null",
            f"{vm_label} clear read-only flags",
        )
    except GuestCommandError as e:
        LOGGER.warning(f"{vm_label}: Set-Disk -IsReadOnly failed (best-effort): {e}")
    return _win_get_shared_drive_letter(ssh_conn, vm_label, volume_label)


def _win_get_shared_drive_letter(ssh_conn: VMSSHConnection, vm_label: str, volume_label: str) -> str:
    """Find the drive letter of the shared volume by its filesystem label.

    Polls with a timeout because Windows may take a moment to mount the
    filesystem after the disk is brought online.

    Args:
        ssh_conn (VMSSHConnection): Active SSH connection to the Windows VM.
        vm_label (str): Label for log messages.
        volume_label (str): NTFS volume label to search for.

    Returns:
        str: Single-character drive letter (e.g., "E").

    Raises:
        GuestCommandError: If the volume is not found within the timeout.
    """

    def _try_get_drive_letter() -> str | None:
        """Poll for the shared volume's drive letter by filesystem label.

        Returns:
            Single-character drive letter, or None if the volume is not yet available.

        Raises:
            GuestCommandError: On real SSH/PowerShell failures (not volume-not-found).
        """
        result = _win_run_powershell(
            ssh_conn,
            f"(Get-Volume -FileSystemLabel '{volume_label}' -ErrorAction SilentlyContinue).DriveLetter",
            f"{vm_label} get shared drive letter",
        ).strip()
        return result if result and len(result) == 1 else None

    drive_letter: str | None = None
    try:
        for sample in TimeoutSampler(
            wait_timeout=_WIN_VOLUME_DISCOVERY_TIMEOUT,
            sleep=_WIN_VOLUME_DISCOVERY_POLL_INTERVAL,
            func=_try_get_drive_letter,
        ):
            if sample:
                drive_letter = sample
                break
    except TimeoutExpiredError as exc:
        raise GuestCommandError(
            f"{vm_label}: Volume with label '{volume_label}' not found after "
            f"{_WIN_VOLUME_DISCOVERY_TIMEOUT}s. Ensure the shared disk was labeled "
            f"by test_label_shared_disk before migration."
        ) from exc

    # Type narrowing: TimeoutSampler guarantees either break (drive_letter set) or TimeoutExpiredError
    assert drive_letter is not None
    LOGGER.info(f"{vm_label}: Shared volume '{volume_label}' is drive {drive_letter}:")
    return drive_letter


def _win_refresh_shared_disk(ssh_conn: VMSSHConnection, vm_label: str, volume_label: str) -> None:
    """Flush NTFS metadata cache via disk offline/online cycle.

    NTFS does not see files written by another VM until the disk is taken
    offline and brought back online. This is the Windows equivalent of
    ``blockdev --flushbufs`` used in the Linux verification.

    Args:
        ssh_conn (VMSSHConnection): Active SSH connection to the Windows VM.
        vm_label (str): Label for log messages.
        volume_label (str): NTFS volume label to locate the disk.

    Raises:
        GuestCommandError: If disk offline/online commands fail.
    """
    disk_num = _win_run_powershell(
        ssh_conn,
        f"(Get-Volume -FileSystemLabel '{volume_label}' | Get-Partition).DiskNumber",
        f"{vm_label} get shared disk number",
    ).strip()
    if not disk_num.isdigit():
        raise GuestCommandError(
            f"{vm_label}: Cannot determine disk number for volume '{volume_label}' (got: '{disk_num}')"
        )
    _win_run_powershell(
        ssh_conn, f"Set-Disk -Number {disk_num} -IsOffline $true | Out-Null", f"{vm_label} disk offline"
    )
    _win_run_powershell(
        ssh_conn, f"Set-Disk -Number {disk_num} -IsOffline $false | Out-Null", f"{vm_label} disk online"
    )
    LOGGER.info(f"{vm_label}: Refreshed shared disk '{volume_label}' (disk {disk_num})")


def _win_write_marker(ssh_conn: VMSSHConnection, drive_letter: str, filename: str, content: str, vm_label: str) -> None:
    """Write a marker file on the SHARED volume.

    Args:
        ssh_conn (VMSSHConnection): Active SSH connection to the Windows VM.
        drive_letter (str): Drive letter (e.g., "E").
        filename (str): File name to write (e.g., "test-vm1.txt").
        content (str): Text content to write.
        vm_label (str): Label for log messages.

    Raises:
        GuestCommandError: If the write command fails.
    """
    ps_path = f"{drive_letter}:\\{filename}".replace("'", "''")
    ps_content = content.replace("'", "''")
    _win_run_powershell(
        ssh_conn,
        f"Set-Content -Path '{ps_path}' -Value '{ps_content}'",
        f"{vm_label} write {filename}",
    )


def _win_read_marker(ssh_conn: VMSSHConnection, drive_letter: str, filename: str, expected: str, vm_label: str) -> None:
    """Read a marker file from the SHARED volume and verify its content.

    Args:
        ssh_conn (VMSSHConnection): Active SSH connection to the Windows VM.
        drive_letter (str): Drive letter (e.g., "E").
        filename (str): File name to read (e.g., "test-vm1.txt").
        expected (str): Expected content substring.
        vm_label (str): Label for log messages.

    Raises:
        GuestCommandError: If the read command fails.
        AssertionError: If content does not match expected data.
    """
    ps_path = f"{drive_letter}:\\{filename}".replace("'", "''")
    content = _win_run_powershell(
        ssh_conn,
        f"Get-Content -Path '{ps_path}'",
        f"{vm_label} read {filename}",
    )
    if expected not in content.strip():
        raise AssertionError(f"{vm_label} cannot read expected data from {filename}: {content}")
    LOGGER.info(f"{vm_label}: Successfully read {filename}")


def _win_do_bidirectional_verification(
    ctx: _SharedDiskContext,
    volume_label: str,
    test_file_vm1: str,
    test_file_vm2: str,
) -> None:
    """Run bidirectional read/write verification between both Windows VMs.

    Args:
        ctx (_SharedDiskContext): Shared disk verification context (SSH connections, VM names).
        volume_label (str): NTFS volume label to locate the shared disk.
        test_file_vm1 (str): Marker filename written by VM1.
        test_file_vm2 (str): Marker filename written by VM2.

    Raises:
        GuestCommandError: If any PowerShell command fails.
        AssertionError: If marker content does not match expected data.
    """
    with ctx.ssh_vm1:
        drive1 = _win_ensure_shared_volume_online(ctx.ssh_vm1, "VM1", volume_label)
        _win_write_marker(ctx.ssh_vm1, drive1, test_file_vm1, _MARKER_VM1_CONTENT, "VM1")

        with ctx.ssh_vm2:
            _win_ensure_shared_volume_online(ctx.ssh_vm2, "VM2", volume_label)  # drive letter re-queried after refresh
            _win_refresh_shared_disk(ctx.ssh_vm2, "VM2", volume_label)
            drive2 = _win_get_shared_drive_letter(ctx.ssh_vm2, "VM2", volume_label)
            _win_read_marker(ctx.ssh_vm2, drive2, test_file_vm1, _MARKER_VM1_CONTENT, "VM2")

            _win_write_marker(ctx.ssh_vm2, drive2, test_file_vm2, _MARKER_VM2_CONTENT, "VM2")
            _win_refresh_shared_disk(ctx.ssh_vm2, "VM2", volume_label)

        _win_refresh_shared_disk(ctx.ssh_vm1, "VM1", volume_label)
        drive1 = _win_get_shared_drive_letter(ctx.ssh_vm1, "VM1", volume_label)
        _win_read_marker(ctx.ssh_vm1, drive1, test_file_vm2, _MARKER_VM2_CONTENT, "VM1")


def verify_shared_disk_data_windows(
    prepared_plan: dict[str, Any],
    vm_ssh_connections: SSHConnectionManager,
    source_provider_data: dict[str, Any],
    ocp_admin_client: DynamicClient,
) -> None:
    """Verify shared disk is accessible from both Windows VMs after migration.

    Uses NTFS volume label to locate the shared disk (no Linux device paths).
    The label is set dynamically by ``label_shared_disk_on_source_windows``
    before migration and read from ``prepared_plan["_shared_disk_label"]``.

    Flow:
    1. Confirm shared PVC exists via KubeVirt volumeStatus
    2. VM1: bring shared disk online, write test data
    3. VM2: bring shared disk online, refresh (clear stale NTFS cache), read VM1's data, write, refresh (flush to disk)
    4. VM1: refresh disk (invalidate cache), read VM2's data

    Args:
        prepared_plan (dict[str, Any]): Heterogeneous pytest fixture dict — schema varies by
            test scenario (uses virtual_machines, source_vms_data, _vm_target_namespace).
        vm_ssh_connections (SSHConnectionManager): SSH connection manager.
        source_provider_data (dict[str, Any]): Heterogeneous provider config from .providers.json —
            schema varies by provider type (uses guest credentials keys).
        ocp_admin_client (DynamicClient): OpenShift admin client for destination VM lookup.

    Raises:
        KeyError: If ``prepared_plan`` does not contain ``"_shared_disk_label"``.
        ValueError: If the stored volume label is not safe for PowerShell usage.
        TimeoutExpiredError: If SSH, PowerShell, or data verification keeps failing until retry timeout.
    """
    volume_label = prepared_plan["_shared_disk_label"]
    if not _SAFE_LABEL_RE.fullmatch(volume_label):
        raise ValueError(f"Invalid volume label: {volume_label}")

    # Shared PVC validated by helper (device paths unused — Windows uses volume labels)
    ctx = _prepare_shared_disk_verification(prepared_plan, vm_ssh_connections, source_provider_data, ocp_admin_client)

    LOGGER.info(f"Verifying Windows shared disk (label='{volume_label}') between {ctx.vm1_name} and {ctx.vm2_name}")

    last_exc: Exception | None = None

    def _attempt() -> bool | None:
        nonlocal last_exc
        try:
            _win_do_bidirectional_verification(ctx, volume_label, _MARKER_VM1_FILENAME, _MARKER_VM2_FILENAME)
            return True
        except (
            SSHException,
            AuthenticationException,
            NoValidConnectionsError,
            ChannelException,
            SSHConnectionSetupError,
            ConnectionError,
            GuestCommandError,
        ) as e:
            last_exc = e
            LOGGER.warning(f"Shared disk verification failed: {type(e).__name__}: {e} - retrying...")
            return None

    try:
        for sample in TimeoutSampler(
            wait_timeout=_WIN_VERIFICATION_RETRY_TIMEOUT,
            sleep=_WIN_VERIFICATION_RETRY_INTERVAL,
            func=_attempt,
        ):
            if sample:
                break
    except TimeoutExpiredError:
        last_err = f" Last error: {type(last_exc).__name__}: {last_exc}" if last_exc else ""
        raise TimeoutExpiredError(
            f"Windows shared disk verification failed after {_WIN_VERIFICATION_RETRY_TIMEOUT}s.{last_err}"
        ) from last_exc

    LOGGER.info("Windows shared disk verification successful - bidirectional access confirmed")
