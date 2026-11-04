# Shared Disk Migration (MTV-4548)

**Location**: `docs/SHARED_DISK_MIGRATION.md`

This document explains the shared disk migration feature implementation, including code workflow, design decisions, and usage.

## Table of Contents
1. [Overview](#overview)
2. [Feature Background](#feature-background)
3. [Code Architecture](#code-architecture)
4. [Test Workflow](#test-workflow)
5. [Code Details](#code-details)
6. [Infrastructure Fixes](#infrastructure-fixes)
7. [Running the Tests](#running-the-tests)
8. [Troubleshooting](#troubleshooting)

## Overview

MTV-4548 implements cold migration of RHEL VMs with shared disks from VMware to OpenShift Virtualization. The feature allows multiple VMs sharing the same disk to be migrated while maintaining the shared disk relationship.

**Key Concept**:
- **Owner VM** migrates the shared disk (creates new PVC)
- **Consumer VM** reuses the owner's shared disk PVC (does not migrate that disk)

## Feature Background

### VMware Shared Disks

In VMware, multiple VMs can share the same VMDK disk file:
- One VM is the "owner" - has read/write access
- Other VMs are "consumers" - share the same disk
- Used for shared storage, clustering, or data sharing between VMs

### OpenShift Shared Disks

In OpenShift Virtualization:
- Shared disk = same PVC mounted by multiple VMs
- Uses `ReadWriteMany` (RWX) access mode
- Storage must support RWX (NFS, CephFS, etc.)

### Migration Challenge

Migrating shared disk VMs requires:
1. Migrate owner VM first - creates shared PVC
2. Migrate consumer VMs - reference owner's PVC instead of creating new ones
3. Verify all VMs can access the shared disk

### Known Limitation (MTV-2200)

virt-v2v cannot update `/etc/fstab` for shared disks. This means:
- Shared disk must be **unmounted** in source VMs before migration
- If mounted, migration succeeds but VM fails to boot (fstab entry causes boot failure)
- After migration, users must manually mount the shared disk

## Code Architecture

```
tests/shared_disk/test_shared_disk_rhel_migration.py          # Main test class (6 test methods)
├─> tests/tests_config/config.py             # Test configuration
├─> utilities/shared_disk.py                 # Shared disk utilities
│   ├─> verify_shared_disk_data()            # Verify disk accessibility
│   └─> verify_pvc_count_for_vm()            # Verify PVC reuse
├─> utilities/mtv_migration.py               # Standard migration utilities
├─> utilities/post_migration.py              # Standard validation
└─> utilities/ssh_utils.py                   # SSH connection management (FIXED)
```

### File Responsibilities

| File | Purpose |
|------|---------|
| `test_shared_disk_rhel_migration.py` | 6-step test orchestration (2 VMs migrated in one plan, PVC verified, data verified) |
| `utilities/shared_disk.py` | Shared disk specific validation logic |
| `tests_config/config.py` | VM names and test parameters |
| `utilities/ssh_utils.py` | SSH infrastructure (CRITICAL FIX for reconnections) |

## Test Workflow

### High-Level Flow

```
1. test_create_storagemap_vm1     → Create storage mapping for VM1
2. test_create_networkmap_vm1     → Create network mapping for VM1
3. test_create_plan_vm1           → Create migration plan for VM1 (owner)
4. test_migrate_vm1               → Migrate VM1 (creates shared PVC)
                                    ↓
5. test_create_storagemap_vm2     → Create storage mapping for VM2
6. test_create_networkmap_vm2     → Create network mapping for VM2
7. test_create_plan_vm2           → Create migration plan for VM2 (consumer)
8. test_migrate_vm2               → Migrate VM2 (reuses VM1's shared PVC)
                                    ↓
9. test_verify_pvc_count          → Verify VM2 plan created fewer PVCs
10. test_verify_shared_disk_data  → Verify both VMs can read/write shared disk
11. test_check_vms                → Standard post-migration validation
```

### Detailed Step-by-Step

#### Steps 1-4: Migrate Owner VM (VM1)

**Step 1: test_create_storagemap_vm1**
```python
# Creates StorageMap for VM1
# Maps source VMware datastore → target OpenShift storage class
storage_map_vm1 = get_storage_migration_map(
    prepared_plan=prepared_plan_vm1,
    source_provider=source_provider,
    target_namespace=target_namespace,
    ...
)
```

**Step 2: test_create_networkmap_vm1**
```python
# Creates NetworkMap for VM1
# Maps source VMware network → target OpenShift network (pod or multus)
network_map_vm1 = get_network_migration_map(
    prepared_plan=prepared_plan_vm1,
    source_provider=source_provider,
    target_namespace=target_namespace,
    multus_network_name=multus_network_name,
    ...
)
```

**Step 3: test_create_plan_vm1**
```python
# Creates Plan CR with migrateSharedDisks: true
# This tells MTV to migrate the shared disk (create new PVC)
plan_vm1 = create_plan_resource(
    prepared_plan=prepared_plan_vm1,
    storage_map=storage_map_vm1,
    network_map=network_map_vm1,
    ...
)
# Plan includes: vms[0].migrateSharedDisks = true
```

**Step 4: test_migrate_vm1**
```python
# Execute migration for VM1
# MTV creates:
# - PVC for OS disk
# - PVC for shared disk (RWX mode)
# - VirtualMachine CR
execute_migration(plan=plan_vm1, ...)
```

**Result**: VM1 migrated with shared disk PVC created.

---

#### Steps 5-8: Migrate Consumer VM (VM2)

**Step 5: test_create_storagemap_vm2**
```python
# Same as VM1, creates StorageMap for VM2
storage_map_vm2 = get_storage_migration_map(
    prepared_plan=prepared_plan_vm2,
    ...
)
```

**Step 6: test_create_networkmap_vm2**
```python
# Same as VM1, creates NetworkMap for VM2
network_map_vm2 = get_network_migration_map(
    prepared_plan=prepared_plan_vm2,
    ...
)
```

**Step 7: test_create_plan_vm2**
```python
# Creates Plan CR with migrateSharedDisks: false
# This tells MTV to reuse existing shared disk PVC from VM1
plan_vm2 = create_plan_resource(
    prepared_plan=prepared_plan_vm2,
    storage_map=storage_map_vm2,
    network_map=network_map_vm2,
    ...
)
# Plan includes: vms[0].migrateSharedDisks = false
```

**Step 8: test_migrate_vm2**
```python
# Execute migration for VM2
# MTV creates:
# - PVC for OS disk
# - NO PVC for shared disk (reuses VM1's shared disk PVC)
# - VirtualMachine CR with reference to VM1's shared PVC
execute_migration(plan=plan_vm2, ...)
```

**Result**: VM2 migrated, reusing VM1's shared disk PVC.

---

#### Steps 9-11: Validation

**Step 9: test_verify_pvc_count**
```python
# Verify VM2 created fewer PVCs than VM1
# VM1 has: OS disk PVC + shared disk PVC = 2 PVCs
# VM2 has: OS disk PVC only = 1 PVC (reuses shared disk)

verify_pvc_count_for_vm(
    ocp_admin_client=ocp_admin_client,
    target_namespace=target_namespace,
    plan_name=plan_vm2.name,
    expected_pvc_count=2,  # OS disk + data disk (not shared)
)
```

**Step 10: test_verify_shared_disk_data**

This is the **critical validation** that proves shared disk works:

```python
verify_shared_disk_data(
    vm1_name="vm1",
    vm2_name="vm2",
    vm_ssh_connections=vm_ssh_connections,
    source_provider_data=source_provider_data,
    vm1_info=prepared_plan_vm1["source_vms_data"]["vm1"],
    vm2_info=prepared_plan_vm2["source_vms_data"]["vm2"],
    shared_disk_device="/dev/vdc",  # Shared disk device path
)
```

Inside `verify_shared_disk_data()`:

```
1. SSH to VM1
   ├─> Mount shared disk (/dev/vdc1 → /mnt/shared_disk)
   ├─> Write test file: echo "Data from VM1" > /mnt/shared_disk/test-vm1.txt
   └─> Unmount (exit SSH session)

2. SSH to VM2
   ├─> Mount shared disk (/dev/vdc1 → /mnt/shared_disk)
   ├─> Read VM1's file: cat /mnt/shared_disk/test-vm1.txt
   ├─> Verify contains "Data from VM1" ✓
   ├─> Write test file: echo "Data from VM2" > /mnt/shared_disk/test-vm2.txt
   └─> Unmount (exit SSH session)

3. SSH to VM1 again
   ├─> Unmount stale mount (CRITICAL FIX - see below)
   ├─> Remount fresh (/dev/vdc1 → /mnt/shared_disk)
   ├─> Read VM2's file: cat /mnt/shared_disk/test-vm2.txt
   ├─> Verify contains "Data from VM2" ✓
   └─> Unmount and cleanup

Result: Bidirectional shared disk access confirmed ✓
```

**Step 11: test_check_vms**
```python
# Standard post-migration validation
# - VMs exist and running
# - SSH connectivity works
# - Network configured correctly
# - Storage attached correctly
check_vms(plan=prepared_plan_vm2, ...)
```

## Code Details

### 1. Test Configuration (tests/tests_config/config.py)

```python
"test_shared_disk_rhel_migration": {
    "virtual_machines": [
        {
            "name": "mtv-feature-shared-rhel1",  # Owner VM
            "source_vm_power": "off",            # Shut down before migration
            "guest_agent": True,                 # VMware tools installed
            "migrate_shared_disks": True,        # ← Migrate shared disk
            "target_power_state": "on",          # Start after migration
        },
        {
            "name": "mtv-feature-shared-rhel2",  # Consumer VM
            "source_vm_power": "off",
            "guest_agent": True,
            "migrate_shared_disks": False,       # ← Reuse shared disk
            "target_power_state": "on",
        },
    ],
    "warm_migration": False,                     # Cold migration only
    "shared_disk_device": "/dev/vdc",            # Shared disk device path
    "expected_pvc_count_vm2": 2,                 # VM2: OS + data (not shared)
    "target_power_state": "on",
}
```

### 2. Shared Disk Utilities (utilities/shared_disk.py)

#### Function: verify_shared_disk_data()

**Purpose**: Verify bidirectional shared disk access between two VMs.

**Parameters**:
- `vm1_name`, `vm2_name`: VM names in OpenShift
- `vm_ssh_connections`: SSHConnectionManager instance
- `source_provider_data`: Provider config (for SSH credentials)
- `vm1_info`, `vm2_info`: VM information (OS type, etc.)
- `shared_disk_device`: Device path (default: `/dev/vdc`)

**Algorithm**:
```python
1. Get SSH credentials from provider config (based on OS type)
   vm1_user, vm1_pass = get_ssh_credentials_from_provider_config(...)
   vm2_user, vm2_pass = get_ssh_credentials_from_provider_config(...)

2. Create SSH connections (via virtctl port-forward)
   ssh_vm1 = vm_ssh_connections.create(vm_name=vm1_name, ...)
   ssh_vm2 = vm_ssh_connections.create(vm_name=vm2_name, ...)

3. VM1: Mount and write
   partition = f"{shared_disk_device}1"  # /dev/vdc1
   mount_point = "/mnt/shared_disk"

   with ssh_vm1:
       sudo mkdir -p /mnt/shared_disk
       sudo mount /dev/vdc1 /mnt/shared_disk
       echo 'Data from VM1' | sudo tee /mnt/shared_disk/test-vm1.txt
       sudo sync

4. VM2: Mount, read VM1's data, write
   with ssh_vm2:
       sudo mkdir -p /mnt/shared_disk
       sudo mount /dev/vdc1 /mnt/shared_disk

       # Read VM1's file
       data = sudo cat /mnt/shared_disk/test-vm1.txt
       assert "Data from VM1" in data  # ✓ VM2 sees VM1's data

       # Write VM2's data
       echo 'Data from VM2' | sudo tee /mnt/shared_disk/test-vm2.txt
       sudo sync

5. VM1: Unmount stale, remount fresh, read VM2's data
   with ssh_vm1:
       # CRITICAL: Unmount old mount to see fresh filesystem
       sudo umount /mnt/shared_disk
       sudo mount /dev/vdc1 /mnt/shared_disk

       # Read VM2's file
       data = sudo cat /mnt/shared_disk/test-vm2.txt
       assert "Data from VM2" in data  # ✓ VM1 sees VM2's data

       sudo umount /mnt/shared_disk

6. Cleanup
   with ssh_vm2:
       sudo umount /mnt/shared_disk
```

**Why unmount/remount in step 5?**

When SSH reconnects to VM1, the mount from step 3 is still active but shows **stale filesystem state** (before VM2's write). Unmounting and remounting forces the kernel to read fresh data from disk.

**Why use `tee` instead of shell redirect?**

Shell redirect (`echo "data" > file`) has quoting issues over SSH. Using `tee` is more reliable:
```bash
# Unreliable (shell quoting issues)
sudo sh -c "echo 'Data' > /mnt/shared_disk/test.txt"

# Reliable
echo 'Data' | sudo tee /mnt/shared_disk/test.txt > /dev/null
```

#### Function: verify_pvc_count_for_vm()

**Purpose**: Verify PVC count matches expected (proves PVC reuse).

**Parameters**:
- `ocp_admin_client`: OpenShift client
- `target_namespace`: Where PVCs are created
- `plan_name`: Plan name (used to filter PVCs)
- `expected_pvc_count`: Expected number of PVCs

**Algorithm**:
```python
1. Get all PVCs in namespace
   all_pvcs = PersistentVolumeClaim.get(
       dyn_client=ocp_admin_client,
       namespace=target_namespace
   )

2. Filter PVCs by plan name (plan name is substring of PVC name)
   plan_pvcs = [pvc for pvc in all_pvcs if plan_name in pvc.name]

3. Count and verify
   actual_count = len(plan_pvcs)
   assert actual_count == expected_pvc_count

   # For VM1 plan: expect 3 PVCs (OS + data + shared)
   # For VM2 plan: expect 2 PVCs (OS + data, NO shared - reuses VM1's)
```

### 3. Test Class (tests/shared_disk/test_shared_disk_rhel_migration.py)

**Class Structure**:
```python
@pytest.mark.parametrize("class_plan_config", [...], indirect=True)
@pytest.mark.usefixtures("cleanup_migrated_vms")
@pytest.mark.incremental
class TestSharedDiskRhelMigration:
    """Test shared disk migration (MTV-676)."""

    # 6 test methods (see workflow above)
    def test_create_storagemap(self, ...): ...
    def test_create_networkmap(self, ...): ...
    def test_create_plan(self, ...): ...
    def test_migrate_vms(self, ...): ...
    def test_verify_shared_disk_data(self, ...): ...
    def test_check_vms(self, ...): ...
```

**Key Points**:
- `@pytest.mark.incremental`: If any test fails, remaining tests are skipped
- `@pytest.mark.usefixtures("cleanup_migrated_vms")`: Auto-cleanup VMs after test class
- Stores resources on class (`self.__class__.storage_map_vm1`) for access across test methods

## Infrastructure Fixes

### Fix 1: SSH Executor Port (utilities/ssh_utils.py)

**Problem**:
When reconnecting to the same VM (as in shared disk test), new SSH executors defaulted to port 22 instead of using the port-forward port.

**Root Cause**:
`rrmngmnt` library creates new executors that don't inherit the port-forward port.

**Fix** (line 250):
```python
class VMSSHConnection:
    def __enter__(self):
        # ... existing code ...

        # CRITICAL: Set port on the executor factory so ALL executors use the port-forward port
        self.rrmngmnt_host.executor_factory.port = self.local_port

        # ... rest of code ...
```

**Impact**: All SSH executors now use the correct port-forward port, enabling multiple reconnections to the same VM.

### Fix 2: virtctl Symlink (utilities/virtctl.py)

**Problem**:
Each pytest worker downloaded virtctl from the cluster, wasting time and bandwidth.

**Fix**:
Create symlink from system-installed virtctl to pytest shared directory:

```python
def _check_existing_virtctl(download_dir: Path) -> Path | None:
    # Check if already in download_dir
    virtctl_binary = download_dir / "virtctl"
    if virtctl_binary.exists():
        return virtctl_binary

    # Check system PATH
    existing_virtctl = shutil.which("virtctl")
    if existing_virtctl:
        # Create symlink for pytest-xdist workers to share
        virtctl_binary.symlink_to(existing_virtctl)
        return virtctl_binary

    return None  # Download from cluster
```

**Impact**: All pytest workers share the same virtctl binary, avoiding redundant downloads.

## Running the Tests

### Prerequisites

1. **Source VMs** (in VMware):
   - Two RHEL VMs with shared disk
   - Shared disk must be `/dev/vdc` (or configure in config.py)
   - VMware tools installed
   - VMs powered off
   - **Shared disk unmounted** (due to MTV-2200 limitation)

2. **OpenShift Cluster**:
   - MTV (Migration Toolkit for Virtualization) installed
   - Storage class supporting RWX (ReadWriteMany)
   - Multus CNI (if using bridged networking)

3. **Configuration**:
   - `.providers.json`: VMware credentials
   - `tests_config/config.py`: VM names updated

### Run Tests

```bash
# Run shared disk test
pytest tests/shared_disk/test_shared_disk_rhel_migration.py -v

# Run with detailed logs
pytest tests/shared_disk/test_shared_disk_rhel_migration.py -v -s

# Skip teardown to inspect resources
pytest tests/shared_disk/test_shared_disk_rhel_migration.py --skip-teardown

# Run specific test method
pytest tests/shared_disk/test_shared_disk_rhel_migration.py::TestSharedDiskRhelMigration::test_verify_shared_disk_data -v
```

### Expected Output

```
tests/shared_disk/test_shared_disk_rhel_migration.py::TestSharedDiskRhelMigration::test_create_storagemap PASSED
tests/shared_disk/test_shared_disk_rhel_migration.py::TestSharedDiskRhelMigration::test_create_networkmap PASSED
tests/shared_disk/test_shared_disk_rhel_migration.py::TestSharedDiskRhelMigration::test_create_plan PASSED
tests/shared_disk/test_shared_disk_rhel_migration.py::TestSharedDiskRhelMigration::test_migrate_vms PASSED
tests/shared_disk/test_shared_disk_rhel_migration.py::TestSharedDiskRhelMigration::test_verify_shared_disk_data PASSED
tests/shared_disk/test_shared_disk_rhel_migration.py::TestSharedDiskRhelMigration::test_check_vms PASSED

======================== 6 of 6 completed, 6 Pass, 0 Fail ========================
```

## Troubleshooting

### Test Fails: VM2 migration fails

**Check**:
```bash
oc get plan -n <namespace>
oc describe plan <plan-vm2-name> -n <namespace>
```

**Common causes**:
- VM1's shared disk PVC not created (check VM1 migration)
- VM1 and VM2 don't reference the same shared disk in VMware
- Storage class doesn't support RWX mode

### Test Fails: test_verify_pvc_count

**Check**:
```bash
oc get pvc -n <namespace>
```

**Expected**:
- VM1 plan PVCs: 3 (OS + data + shared)
- VM2 plan PVCs: 2 (OS + data, NO shared)

**If VM2 has 3 PVCs**: MTV created a new shared disk PVC instead of reusing VM1's - check plan VM2 has `migrateSharedDisks: false`.

### Test Fails: test_verify_shared_disk_data - VM cannot mount

**Error**: `mount: /mnt/shared_disk: special device /dev/vdc1 does not exist`

**Cause**: Shared disk device path is wrong.

**Fix**: Check actual device path in VM:
```bash
virtctl console vm/<vm-name> -n <namespace>
lsblk  # Find shared disk device
```

Update `shared_disk_device` in config.py.

### Test Fails: test_verify_shared_disk_data - File not found

**Error**: `cat: /mnt/shared_disk/test-vm2.txt: No such file or directory`

**Cause**: Stale mount issue (fixed by unmount/remount).

**Verify fix is applied**: Check `utilities/shared_disk.py` line 97-98 has unmount/remount code.

### Test Fails: SSH connection timeout

**Check**:
```bash
# Check virtctl port-forward is running
ps aux | grep "virtctl port-forward"

# Check VM is running
oc get vm -n <namespace>

# Check VM has IP address
oc get vmi -n <namespace>
```

**Common causes**:
- VM not fully booted
- Guest agent not running
- Firewall blocking SSH (port 22)

### Migration Hangs

**Check forklift controller logs**:
```bash
oc logs -n openshift-mtv deployment/forklift-controller -f
```

**Check virt-v2v pod logs**:
```bash
# Find virt-v2v pod for migration
oc get pods -n <namespace> | grep virt-v2v

# View logs
oc logs -n <namespace> <virt-v2v-pod-name> -f
```

## Windows Shared Disk Migration (MTV-4639)

### Overview

Windows shared disk migration uses the same `migrateSharedDisks` VM-level override but with
a different approach for disk identification. Unlike Linux (which uses device paths like `/dev/vdc`),
Windows has no stable device paths — drive letters are assigned dynamically and can change across
reboots or migrations.

### How It Works: Dynamic NTFS Volume Labeling

The test dynamically labels the shared disk **before migration** using VMware Guest Operations API:

1. **Pre-migration labeling** (`test_label_shared_disk`):
   - VMware API identifies the shared disk by SCSI controller position
   - Guest Operations API runs PowerShell inside the source VM
   - PowerShell matches the disk by WMI serial number (`Win32_DiskDrive.SerialNumber`)
   - Sets a unique NTFS volume label: `SHARED-{session_uuid}` (e.g., `SHARED-auto-egpg`)

2. **Post-migration verification** (`test_verify_shared_disk_data`):
   - Finds the shared volume by label using `Get-Volume -FileSystemLabel`
   - Brings the disk online (Windows SAN Policy may leave shared disks offline)
   - VM1 writes a marker file, VM2 reads it (and vice versa)
   - NTFS cache invalidation via disk offline/online cycling between reads

### Code Architecture

```
tests/shared_disk/test_shared_disk_windows_migration.py    # 7-step test class
├─> utilities/shared_disk.py
│   ├─> label_shared_disk_on_source_windows()              # Pre-migration: label via Guest Ops
│   ├─> verify_shared_disk_data_windows()                  # Post-migration: bidirectional verification
│   ├─> _win_ensure_shared_volume_online()                 # Bring disk online + find drive letter
│   ├─> _win_get_shared_drive_letter()                     # Poll for volume by label
│   ├─> _win_refresh_shared_disk()                         # NTFS cache flush (offline/online cycle)
│   ├─> _win_write_marker() / _win_read_marker()           # Marker file operations
│   └─> _win_run_powershell()                              # SSH PowerShell execution
├─> utilities/vmware_guest_operations.py                   # Guest Ops API (run_command_in_vmware_guest)
└─> tests/tests_config/config.py                           # VM config
```

### Test Flow (7 Steps)

```
1. test_label_shared_disk        → Label shared NTFS volume via VMware Guest Ops
2. test_create_storagemap        → Create storage mapping for both VMs
3. test_create_networkmap        → Create network mapping for both VMs
4. test_create_plan              → Create single plan with VM-level migrateSharedDisks overrides
5. test_migrate_vms              → Execute cold migration
6. test_verify_shared_disk_data  → Bidirectional read/write verification via SSH
7. test_check_vms                → Standard post-migration validation
```

### Test Configuration

```python
"test_shared_disk_windows_migration": {
    "virtual_machines": [
        {"name": "mtv-tests-shared-win1", "source_vm_power": "off",
         "guest_agent": True, "migrate_shared_disks": True},    # Owner
        {"name": "mtv-tests-shared-win2", "source_vm_power": "off",
         "guest_agent": True, "migrate_shared_disks": False},   # Consumer
    ],
    "warm_migration": False,
    "migrate_shared_disks": True,
    "target_power_state": "on",
}
```

### Prerequisites (Windows)

1. **Source VMs**: Two Windows Server VMs sharing a disk in VMware
   - VMware Tools installed (required for Guest Operations API)
   - VMs powered off before migration
   - No pre-configured labels required — the test labels the disk dynamically
2. **OpenShift Cluster**: MTV installed, RWX-capable storage class
3. **Provider config**: `.providers.json` must include `windows_username` and `windows_password`

### Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| Dynamic labeling via Guest Ops | No manual VM preparation needed; unique per session |
| NTFS label (not device path) | Survives disk re-attachment across hypervisors |
| `shortuuid` in label | 16 chars total, within NTFS 32-char limit |
| Disk offline/online for cache flush | Only reliable way without admin privileges |
| TimeoutSampler with SSH retry | Windows SSH via port-forward is flaky |
| `-ErrorAction SilentlyContinue` | Separates "volume not found yet" from real SSH errors |

### Troubleshooting (Windows)

**Shared volume not found after migration:**
- Check the label was set: test logs should show `Labeled disk N drive X: as SHARED-auto-xxxx`
- Verify the disk is online: `Get-Disk | Where-Object {$_.OperationalStatus -eq 'Offline'}`
- Check Windows SAN Policy: `Get-StorageSetting | Select NewDiskPolicy`

**SSH timeout during verification:**
- The test retries for 300s with 15s intervals — check logs for the last error
- Verify VM is running: `oc get vmi -n <namespace>`
- Check Windows SSH service: `Get-Service sshd` inside the VM

**Migration fails after labeling:**
- Ensure `shutdown_vm_guest()` completed (not `stop_vm()` which hard-powers-off)
- Check for Windows Update blocking shutdown
- Verify VMware Tools are running for graceful shutdown

## Summary

The shared disk migration feature enables migrating VMs with shared disks from VMware to OpenShift. Key aspects:

1. **RHEL test** (MTV-4548): 6-step pattern, device paths (`/dev/vdc`), mount/unmount verification
2. **Windows test** (MTV-4639): 7-step pattern, dynamic NTFS labeling via Guest Ops, drive letter discovery
3. **Single plan**: Both VMs migrated in one plan with VM-level `migrateSharedDisks` overrides
4. **Bidirectional access test**: Both VMs can read/write the shared disk after migration
5. **Known limitation (Linux)**: Shared disk must be unmounted before migration (MTV-2200)
