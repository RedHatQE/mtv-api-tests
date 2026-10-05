# Advanced Plan Features

Advanced plan options control how MTV builds the destination VM once the provider, storage, and network mappings are in
place. In this project you declare them under `tests_params` in `tests/tests_config/config.py` and pass them into
`create_plan_resource()` from your test class.

This page covers three layers:

| Layer | Where it lives | Examples |
| --- | --- | --- |
| VM-level options | one entry per item in `virtual_machines` | `luks`, `disk_type`, `add_nic`, `migrate_shared_disks` |
| Plan-level options | top level of a `tests_params` entry | `preserve_static_ips`, `xfs_compatibility`, `per_nic_network_map` |
| Test-verification options | top level, consumed by the post-migration step | `xfs_check` |

> **Note:** Every plan-level key is read with `plan.get(...)` in the preparation fixtures and with an explicit keyword
> argument in `create_plan_resource()`, so an option only reaches the MTV Plan CR when the test class actually passes it.

## Plan-level options

| Key | What it controls | Where it is applied | Example |
| --- | --- | --- | --- |
| `target_power_state` | Final destination VM power state | Plan CR field | `"on"` |
| `preserve_static_ips` | Preserve guest static IP configuration | Plan CR field + post-migration SSH check | `True` |
| `enable_nested_virtualization` | `False` disables the `vmx`/`svm` CPU features | Plan CR field + CPU-feature check | `False` |
| `pvc_name_template` | Destination PVC naming template (string or provider-keyed map) | Plan CR field | see [PVC naming templates](#pvc-naming-templates) |
| `pvc_name_template_use_generate_name` | Let Kubernetes append a random PVC suffix | Plan CR field | `True` |
| `vm_target_namespace` | Namespace where migrated VMs are created | Plan CR `targetNamespace` | `f"mtv-vms-cold-comprehensive-{uuid4hex}"` |
| `multus_namespace` | Namespace where extra NADs are created | `multus_network_name` fixture | `"default"` |
| `target_node_selector` | Single node label the VM must be scheduled onto | Plan CR field | `{"mtv-comprehensive-node": None}` |
| `target_labels` | Labels added to the migrated VM template | Plan CR field | `{"static-label": "static-value"}` |
| `target_affinity` | Affinity rules applied to the migrated VM | Plan CR field | `{"podAffinity": {...}}` |
| `migrate_shared_disks` | Enable shared-disk migration for every VM by default | Plan CR field | `True` |
| `xfs_compatibility` | Use the XFS-compatible virt-v2v image | Plan CR field | `True` |
| `rdm_as_lun` | Map RDM disks as SCSI LUN devices instead of virtio | Plan CR field | `True` |
| `copyoffload` | Enable copy-offload (XCOPY) handling; forces `pvc_name_template` to `"pvc"` | Plan CR field | `True` |
| `run_preflight_inspection` | Enable Forklift preflight Deep Inspection on warm migrations | Plan CR field | `False` |
| `guest_agent_timeout` | Seconds to wait for the destination guest agent | Post-migration VM lookup | `600` |
| `inventory_timeout` | Seconds to wait for cloned VMs and added NICs in the Forklift inventory | `prepared_plan` fixture (default `300`) | `600` |
| `clone_to_same_host` | Pin VM 2+ to the ESXi host used by VM 1 | `prepared_plan` fixture | `True` |
| `pin_to_non_dedicated_host` | Pin every VM to an ESXi host outside `copyoffload.dedicated_migration_hosts` | `prepared_plan` fixture | `True` |
| `disable_drs_for_vms` | Disable vSphere DRS per VM after cloning | `prepared_plan` fixture | `True` |
| `per_nic_network_map` | One NetworkMap entry per NIC instead of per distinct network | `get_network_migration_map()` | `True` |
| `skip_clone` | Skip the cloning phase and migrate the existing VMs | `prepared_plan` fixture | `True` |
| `warm_migration` | Run a warm migration | Plan CR field | `True` |

> **Warning:** `disable_drs_for_vms` raises `ValueError` for OVA providers and for any clone provider that is not
> `VMWareProvider`. `skip_clone` raises `ValueError` on providers whose `supports_skip_clone()` is `False` (RHV, whose
> plan names are templates) and when combined with `disable_drs_for_vms`, `clone_to_same_host`, `preserve_static_ips`
> (non-Hyper-V), `migrate_shared_disks`, or `add_nic`.

## VM-level options

Each entry of `virtual_machines` describes one source VM or template.

| Key | What it controls | Example |
| --- | --- | --- |
| `name` | Source VM or template name | `"mtv-tests-rhel9-luks"` |
| `source_vm_power` | Power state applied to the source VM before migration (`"on"` or `"off"`) | `"on"` |
| `guest_agent` | Wait for the destination guest agent and assert it is running | `True` |
| `clone` | Clone the source VM before migrating it | `True` |
| `clone_name` | Override the generated clone base name | `"XCopy_Test_VM_CAPS"` |
| `preserve_name_format` | Keep uppercase and underscores in `clone_name` instead of sanitizing | `True` |
| `disk_type` | VMware provisioning for the cloned disks | `"thick-eager"` |
| `add_disks` | Extra disks to attach to the cloned source VM | `[{"size_gb": 30, "provision_type": "thin"}]` |
| `snapshots` | Snapshots to create on the source VM before migration | `2` |
| `target_datastore_id` | Target datastore for the clone | `"non_xcopy_datastore_id"` |
| `luks` | Marks the VM as having a LUKS-encrypted disk | `True` |
| `luks_passphrase` | Per-VM LUKS passphrase override | `"WRONGPASSWORD"` |
| `migrate_shared_disks` | `True` for the shared-disk owner VM, `False` for the consumer | `True` |
| `add_nic` | Attach an extra NIC to the clone (vSphere only) | `True` |
| `add_nic_start_connected` | Required when `add_nic` is `True`; `False` yields a disconnected NIC | `False` |
| `win_os` | Hyper-V guest OS override; must be a bool | `False` |

> **Note:** `add_nic` skips the whole class on non-vSphere providers and raises `ValueError` when `add_nic_start_connected`
> is missing or is not a bool. The MAC of the added NIC is written back into the VM dict as `connected_nic_mac` or
> `disconnected_nic_mac` and is checked after migration.

### Disk provisioning with `disk_type`

`disk_type` maps to a vSphere relocate transform. `thick-eager` additionally sets per-disk `DiskLocator` entries with
eager scrubbing:

```python
DISK_TYPE_MAP = {
    "thin": ("sparse", "Setting disk provisioning to 'thin' (sparse)."),
    "thick-lazy": ("flat", "Setting disk provisioning to 'thick-lazy' (flat)."),
    "thick-eager": ("flat", "Setting disk provisioning to 'thick-eager' (flat + eagerlyScrub)."),
}
```

An unrecognized value logs a warning and falls back to the vSphere default.

## Test-verification options

| Key | Required | What it controls |
| --- | --- | --- |
| `xfs_check` | Required by XFS tests | Block of verification settings for the XFS step |
| `xfs_check.command` | With `xfs_check` | Absolute path of the command to run in the guest |
| `xfs_check.mount_point` | With `xfs_check` | Mount point passed as the command argument |
| `xfs_check.expected_output` | With `xfs_check` | Substring that must appear in the command output |

## Step patterns

Base pattern: `test_create_storagemap` → `test_create_networkmap` → `test_create_plan` → `test_migrate_vms` →
`test_check_vms`. Feature suites insert or replace steps:

| Suite | Ordered steps | Class |
| --- | --- | --- |
| Base 5-step | storagemap → networkmap → plan → migrate → check_vms | most suites |
| LUKS (6-step) | storagemap → networkmap → plan → migrate → `test_verify_luks_encryption` → check_vms | `TestLuksColdMigration` |
| XFS (6-step) | storagemap → networkmap → plan → migrate → `test_verify_xfs_version` → check_vms | `TestColdMigrationXfs`, `TestWarmMigrationXfs` |
| Shared disk, Linux (6-step) | storagemap → networkmap → plan → migrate → `test_verify_shared_disk_data` → check_vms | `TestSharedDiskRhelMigration` |
| Shared disk, Windows (7-step) | `test_label_shared_disk` → base 5 steps → `test_verify_shared_disk_data` → check_vms | `TestSharedDiskWindowsMigration` |
| Plan archive (6-step) | storagemap → networkmap → plan → migrate (expected failure) → `test_archive_and_delete_plan` → `test_verify_pvc_cleanup` | `TestPlanArchivePvcCleanup` |
| Plan readiness (3-step) | storagemap → networkmap → plan, no migration | `TestColdDualNicSameNetworkPlanValidation` |

## Typical usage

A full example already exists in `tests/tests_config/config.py`:

```python
"test_warm_migration_comprehensive": {
    "virtual_machines": [
        {
            "name": "mtv-win2022-ip-3disks",
            "source_vm_power": "on",
            "guest_agent": True,
        },
    ],
    "warm_migration": True,
    "target_power_state": "on",
    "preserve_static_ips": True,
    "enable_nested_virtualization": False,
    "vm_target_namespace": f"mtv-vms-warm-comprehensive-{uuid.uuid4().hex[:4]}",
    "multus_namespace": "default",  # Cross-namespace NAD access
    # Keys must be Provider.ProviderType string values (e.g. "vsphere") or "default".
    # vsphere uses .FileName; default truncates VmName to stay within 63 chars.
    "pvc_name_template": {
        "vsphere": '{{ .FileName | trimSuffix ".vmdk" | replace "_" "-" }}-{{.DiskIndex}}',
        "default": '{{ .VmName | trunc 32 | trimSuffix "-" }}-{{ .VmName | trunc -4 }}-disk-{{.DiskIndex}}',
    },
    "pvc_name_template_use_generate_name": True,
    "target_labels": {
        "mtv-comprehensive-test": None,  # None = auto-generate with session_uuid
        "static-label": "static-value",
    },
    "target_affinity": {
        "podAffinity": {
            "preferredDuringSchedulingIgnoredDuringExecution": [
                {
                    "podAffinityTerm": {
                        "labelSelector": {"matchLabels": {"app": "comprehensive-test"}},
                        "topologyKey": "kubernetes.io/hostname",
                    },
                    "weight": 75,
                }
            ]
        }
    },
    "guest_agent_timeout": 600,
},
```

That configuration is then passed into the Plan helper in `tests/warm/test_warm_migration_comprehensive.py`:

```python
self.__class__.plan_resource = create_plan_resource(
    ocp_admin_client=ocp_admin_client,
    fixture_store=fixture_store,
    source_provider=source_provider,
    destination_provider=destination_provider,
    storage_map=self.storage_map,
    network_map=self.network_map,
    virtual_machines_list=prepared_plan["virtual_machines"],
    target_power_state=prepared_plan["target_power_state"],
    target_namespace=target_namespace,
    warm_migration=prepared_plan["warm_migration"],
    preserve_static_ips=prepared_plan["preserve_static_ips"],
    vm_target_namespace=prepared_plan["vm_target_namespace"],
    pvc_name_template=prepared_plan["pvc_name_template"],
    pvc_name_template_use_generate_name=prepared_plan["pvc_name_template_use_generate_name"],
    target_labels=target_vm_labels["vm_labels"],
    target_affinity=prepared_plan["target_affinity"],
    enable_nested_virtualization=prepared_plan["enable_nested_virtualization"],
)
```

## Target power state

Use `target_power_state` when you want the migrated VM to end in a known power state, regardless of how the source VM
started out. The repository examples use both `"on"` and `"off"`.

If you omit `target_power_state`, `check_vms_power_state()` falls back to the VM's pre-migration source power state.

> **Note:** `source_vm_power` and `target_power_state` are different. `source_vm_power` controls how the source VM is
> prepared before migration. `target_power_state` controls the expected state of the destination VM after migration.

Practical examples from `tests/tests_config/config.py`:

- `"target_power_state": "on"` in both comprehensive migration tests
- `"target_power_state": "off"` in `test_post_hook_retain_failed_vm` and `test_plan_archive_pvc_cleanup`

## Static IP preservation

Set `preserve_static_ips` to `True` when you want MTV to preserve guest static IP settings. Both comprehensive migration
configs enable it.

For vSphere sources, the provider records guest IP origin and treats `origin == "manual"` as a static IP:

```python
if hasattr(ip_info, "origin"):
    ip_config["ip_origin"] = ip_info.origin
    ip_config["is_static_ip"] = ip_info.origin == "manual"
```

VMware does not always report an origin for Linux guests, so the preparation flow falls back to `nmcli device show`
through VMware Guest Operations and marks the recorded origins as `manual` or `auto` from the guest's own answer.

After migration, `check_static_ip_preservation()` connects to the destination VM over SSH, reads the guest network
configuration (`ipconfig /all` on Windows, `nmcli device show` on Linux), and asserts that every recorded static IP
appears, then validates subnet masks and gateways.

> **Warning:** Static IP verification runs only for source providers in `_STATIC_IP_PROVIDERS` — vSphere and Hyper-V.
> For Linux VMs migrated from vSphere, a failure to detect IP origins through Guest Operations raises `ValueError`
> instead of degrading to a warning when `preserve_static_ips` is enabled.

> **Note:** If no static interfaces are found on the source VM, the check raises `ValueError` telling you to power the
> VM on so guest tools can report IP origins. `preserve_static_ips` also requires `source_vm_power: "on"` on every VM in
> the plan, enforced during preparation.

## PVC naming templates

Use `pvc_name_template` when you want stable, readable PVC names. `resolve_pvc_name_template()` accepts either a plain
template string or a map keyed by provider type, resolved against the source provider type and falling back to
`"default"`.

Real examples from `tests/tests_config/config.py`:

```python
"pvc_name_template": {
    "vsphere": '{{ .FileName | trimSuffix ".vmdk" | replace "_" "-" }}-{{.DiskIndex}}',
    "default": '{{ .VmName | trunc 32 | trimSuffix "-" }}-{{ .VmName | trunc -4 }}-disk-{{.DiskIndex}}',
},
"pvc_name_template_use_generate_name": True,
```

```python
"pvc_name_template": '{{ .VmName | trunc 32 | trimSuffix "-" }}-{{ .VmName | trunc -4 }}-disk-{{.DiskIndex}}',
"pvc_name_template_use_generate_name": False,
```

The validator in `utilities/post_migration.py` explicitly supports:

- `{{.VmName}}`
- `{{.DiskIndex}}`
- `{{.FileName}}`
- Sprig functions such as `trimSuffix`, `replace`, `lower`, and `upper`

When `pvc_name_template_use_generate_name` is `True`, the project expects Kubernetes to append a random suffix and
validates the PVC by prefix match. When it is `False`, the validator expects an exact name match.

> **Note:** The validator also accounts for Kubernetes name-length limits by truncating the rendered template before
> comparing it with the actual PVC name.

> **Warning:** Templates using `{{.FileName}}` need VMDK filenames from a vSphere inventory. On any other provider the
> PVC-name verifier logs a warning and skips that validation path.

There is one important exception for copy-offload migrations. In `utilities/mtv_migration.py`, `copyoffload=True`
overrides any custom template:

```python
if copyoffload:
    plan_kwargs["pvc_name_template"] = "pvc"
```

> **Warning:** If you are running copy-offload tests, do not expect your custom `pvc_name_template` to be used. The helper
> forces it to `"pvc"` so the volume populator framework gets predictable PVC prefixes.

## Custom VM namespaces

Use `vm_target_namespace` when you want the migrated VM to land in a namespace that is different from the namespace where
the Plan and mapping resources are created.

In this project:

- The Plan, `StorageMap`, and `NetworkMap` are still created in the regular `target_namespace`
- The migrated VM itself is created in `vm_target_namespace`
- The `prepared_plan` fixture creates that namespace automatically if it does not already exist, and stores it in
  `plan["_vm_target_namespace"]` for the post-migration lookups
- The comprehensive configs build the value from `uuid.uuid4().hex[:4]` so parallel sessions never collide

> **Note:** `vm_target_namespace` and `multus_namespace` solve different problems. Use `vm_target_namespace` to choose
> where migrated VMs land, and `multus_namespace` to choose where the test-created NetworkAttachmentDefinitions live.

## Node selectors, labels, and affinity

These options control where the migrated VM runs and what metadata MTV applies to it.

A cold-migration example from `tests/cold/test_cold_migration_comprehensive.py` shows the scheduling-related arguments
passed into the Plan helper:

```python
target_node_selector = ({labeled_worker_node["label_key"]: labeled_worker_node["label_value"]},)
target_labels = (target_vm_labels["vm_labels"],)
target_affinity = (prepared_plan["target_affinity"],)
```

### Node selectors

Use `target_node_selector` to place the destination VM on nodes with a matching label. It must contain exactly one label.

Example from `tests/tests_config/config.py`:

```python
"target_node_selector": {
    "mtv-comprehensive-node": None,
},
```

In the comprehensive cold test, the `labeled_worker_node` fixture picks the worker node with the most available memory,
applies the label through a `ResourceEditor` context manager so cleanup is automatic, and passes the resolved selector
into `create_plan_resource()`. After migration, `check_vm_node_placement()` verifies the VM actually landed on that node.

> **Tip:** When the selector value is `None`, the fixture appends a `session_uuid` suffix to the label **key** and uses
> `session_uuid` as the **value**, so two parallel sessions never overwrite each other's label.

### Labels

Use `target_labels` to stamp metadata onto the migrated VM template. Examples from `tests/tests_config/config.py`:

- `"static-label": "static-value"`
- `"mtv-comprehensive-test": None`
- `"test-type": "comprehensive"`

The `target_vm_labels` fixture replaces every `None` value with the current `session_uuid`, the post-migration
`check_vm_labels()` verifies each expected label is present with the expected value, and a missing `target_labels` key in
the test config raises `ValueError` naming the key you forgot.

### Affinity

Use `target_affinity` when you need Kubernetes scheduling preferences or constraints on the migrated VM. The repository
examples use `podAffinity` with `preferredDuringSchedulingIgnoredDuringExecution`, matching VMs near pods with a
specific label and topology key. Because `check_vm_affinity()` deep-compares the resulting block, keep the structure in
your test config exactly as MTV should apply it.

> **Note:** Labels are validated from the VM template metadata, affinity from the VM template spec, and node placement
> from the running VMI's assigned node.

## Multus setup

Multus support is handled as part of network-map preparation, not as a direct Plan field.

The default Multus CNI configuration comes from `conftest.py`:

```python
@pytest.fixture(scope="session")
def multus_cni_config() -> str:
    bridge_type_and_name = "cnv-bridge"
    config = {"cniVersion": "0.3.1", "type": f"{bridge_type_and_name}", "bridge": f"{bridge_type_and_name}"}
    return json.dumps(config)
```

When the source VM has multiple NICs, `utilities/utils.py` maps the first source network to the pod network and every
additional network to a Multus NAD:

```python
for index, network in enumerate(networks):
    if pod_only or index == 0:
        # First network or pod_only mode → pod network
        _destination = _destination_pod
    else:
        multus_network_name_str = multus_network_name["name"]
        multus_namespace = multus_network_name["namespace"]

        nad_name = f"{multus_network_name_str}-{multus_counter}"

        _destination = {
            "name": nad_name,
            "namespace": multus_namespace,
            "type": "multus",
        }
        multus_counter += 1  # Increment for next NAD
```

What this means in practice:

- The first NIC stays on the pod network
- Each additional NIC gets its own `NetworkAttachmentDefinition`
- NADs are named from a short base name plus `-1`, `-2`, and so on
- `multus_namespace` decides where those NADs are created, and the fixture creates that namespace if needed

Both comprehensive configs use:

```python
"multus_namespace": "default",
```

> **Note:** A single-NIC VM does not need Multus. The fixture calculates the number of NADs as `max(0, len(networks) - 1)`,
> plus one per VM that sets `add_nic`, so only additional NICs get Multus attachments.

## Per-NIC network mapping

By default the NetworkMap holds one entry per **distinct source network**, which collapses two NICs attached to the same
vSphere network into a single mapping. Set `per_nic_network_map: True` to emit one mapping per NIC instead, using
`get_per_nic_networks()` with `deduplicate=False`.

This is what lets two NICs on the same source network land on different destination NADs without Forklift rejecting the
plan:

```python
"test_cold_dual_nic_same_network_migration": {
    "virtual_machines": [
        {
            "name": "mtv-tests-rhel9-dual-nic",
        },
    ],
    "warm_migration": False,
    "per_nic_network_map": True,
    "skip_clone": True,
},
```

`TestColdDualNicSameNetworkPlanValidation` is a 3-step plan-readiness test: `test_create_storagemap`,
`test_create_networkmap`, `test_create_plan`. No migration runs, so the proof is that `create_plan_resource()` waits for
Plan Ready without a `VMDuplicateNADMappings` validation error.

> **Note:** The flag is read in two places. The `multus_network_name` fixture uses it to decide how many NADs to create,
> and `get_network_migration_map(per_nic_network_map=...)` uses it to decide whether to deduplicate networks.

## Adding a NIC to the source VM

`add_nic: True` clones the source VM and attaches one extra NIC by duplicating an existing secondary NIC, which
guarantees the new NIC lands on a valid, migratable secondary network. `add_nic_start_connected` is required and decides
whether the new NIC is connected.

Because the NIC is added after the clone was already synced to the Forklift inventory, the preparation flow records the
expected NIC count per VM and then forces a refresh with `wait_for_added_nics_in_forklift_inventory()`. Without that
wait, NetworkMap creation would use stale inventory and Forklift would drop the added NIC.

> **Tip:** `add_nic` requires an existing secondary NIC on the source VM — the VMware implementation copies the backing
> of the first NIC that is not on the pod network and returns `None` when there is none.

## LUKS encrypted disk migration

**Problem.** Forklift cannot read a LUKS-encrypted disk. MTV needs the passphrase as a Kubernetes Secret referenced from the
Plan, and the migrated VM must come up with encryption still active.

**Config keys.**

| Scope | Key | Meaning |
| --- | --- | --- |
| VM | `luks` | `True` marks the VM as having a LUKS-encrypted disk |
| VM | `luks_passphrase` | Per-VM passphrase override |
| Provider | `luks_passphrase` | Optional vSphere source-provider fallback in `.providers.json` |

Real config from `tests/tests_config/config.py`:

```python
"test_luks_cold_migration": {
    "virtual_machines": [
        {
            "name": "mtv-tests-rhel9-luks",
            "source_vm_power": "on",
            "guest_agent": True,
            "luks": True,
            "clone": True,
        },
    ],
    "warm_migration": False,
},
```

The negative case overrides the passphrase per VM instead of the provider entry:

```python
"test_luks_cold_migration_wrong_key": {
    "virtual_machines": [
        {
            "name": "mtv-tests-rhel9-luks",
            "luks": True,
            "luks_passphrase": "WRONGPASSWORD",
            "clone": True,
        },
    ],
    "warm_migration": False,
},
```

**Secret setup.** The `luks_vm_specs` fixture in `tests/luks/conftest.py` does all of it:

1. `populate_vm_ids()` so each VM dict carries its Forklift inventory ID
2. Pop `luks` and `luks_passphrase` off the VM config
3. Resolve the passphrase: per-VM override first, then `source_provider_data["luks_passphrase"]`; raise `ValueError`
   when neither yields a value
4. Create a `Secret` in `target_namespace` with a single `key` string-data entry holding the passphrase, via `create_and_store_resource()`
5. Set `vm["luks"] = {"name": secret.name}` so the Plan references it

**Step pattern (6 steps).** `tests/luks/test_luks_cold_migration.py` splits shared setup into `LuksColdMigrationBase`
(`test_create_storagemap`, `test_create_networkmap`, `test_create_plan`), where `test_create_plan` passes
`virtual_machines_list=luks_vm_specs`. The subclasses then add:

- `TestLuksColdMigration`: `test_migrate_vms` → `test_verify_luks_encryption` → `test_check_vms`
- `TestLuksColdMigrationWrongKey`: `test_migrate_vms` only, wrapped in
  `pytest.raises(MigrationPlanExecError, match="ImageConversion")`

Both classes carry `@pytest.mark.vsphere` and `@pytest.mark.tier1`.

**What is verified.** `verify_luks_encryption()` opens an SSH connection to the migrated VM, runs `lsblk -J -f`,
recursively walks the blockdevice tree for `fstype == "crypto_LUKS"`, and asserts at least one encrypted device was found.
SSH or parse failures are treated as transient and retried every 15 seconds for up to 300 seconds; an empty device list
is definitive and fails immediately.

> **Warning:** The source VM must have its LUKS key file configured (for example `/etc/luks-key` in `/etc/crypttab`) so
> the guest boots unattended. Without it the VM prompts interactively for the passphrase and SSH never comes up, so the
> verification step times out rather than failing on the migration itself.

> **Note:** The wrong-key class matches on `ImageConversion`, the pipeline step name read from
> `plan.instance.status`. If upstream renames that step the match string needs updating.

## XFS v4 filesystem compatibility

**Problem.** The default virt-v2v image cannot handle XFS v4 metadata. MTV's `xfs_compatibility` option selects the
XFS-compatible virt-v2v image instead.

**Config keys.**

```python
"test_warm_migration_xfs": {
    "virtual_machines": [
        {
            "name": "mtv-feature-rhel7-xfs",
            "source_vm_power": "on",
            "guest_agent": True,
        },
    ],
    "warm_migration": True,
    "target_power_state": "on",
    "xfs_compatibility": True,
    "xfs_check": {
        "command": "/usr/sbin/xfs_info",
        "mount_point": "/",
        "expected_output": "crc=0",
    },
},
```

`test_cold_migration_xfs` uses the same shape with `mount_point: "/sdb1"` for a non-root XFS volume. Both test classes are
`TestWarmMigrationXfs` and `TestColdMigrationXfs`.

**Plan wiring.** `create_plan_resource(xfs_compatibility=...)` sets the Plan CR field only when the flag is truthy, so a
`False` value leaves the Forklift default untouched.

**Step pattern (6 steps).** storagemap → networkmap → plan → migrate → `test_verify_xfs_version` → `test_check_vms`.

**What is verified.** `test_verify_xfs_version` reads `prepared_plan["xfs_check"]` with direct key access, looks up each
source VM's info, and calls `check_vm_command_output()` with `command=[xfs_check["command"], xfs_check["mount_point"]]`.
That helper resolves the destination VM name, resolves guest credentials from `.providers.json`, runs the command over
SSH, and asserts `expected_output` appears in stdout.

> **Note:** `check_vm_command_output()` raises `ValueError` for Windows guests, so `xfs_check` is a Linux-only option.

## Shared disk migration

**Problem.** Two vSphere VMs can share one VMDK. Cloning breaks that relationship because each clone gets an independent
disk copy, and Forklift would otherwise migrate a separate disk for every VM.

**Config keys.** `migrate_shared_disks` exists at both scopes:

| Scope | Value | Meaning |
| --- | --- | --- |
| Plan | `True` | Enable shared-disk migration for all VMs by default |
| VM | `True` | This VM is the **owner** and its shared-disk PVC is migrated |
| VM | `False` | This VM is the **consumer** and skips the shared-disk PVC |

Real config from `tests/tests_config/config.py`:

```python
"test_shared_disk_rhel_migration": {
    "virtual_machines": [
        {
            "name": "mtv-feature-shared-rhel1",
            "source_vm_power": "off",
            "guest_agent": True,
            "migrate_shared_disks": True,
        },
        {
            "name": "mtv-feature-shared-rhel2",
            "source_vm_power": "off",
            "guest_agent": True,
            "migrate_shared_disks": False,
        },
    ],
    "warm_migration": False,
    "migrate_shared_disks": True,
    "target_power_state": "on",
},
```

**Preparation.** The `prepared_plan` fixture detects shared-disk configuration from either scope, fails fast when the
source provider does not implement `relink_shared_disks()`, and after all clones complete calls
`relink_shared_disks(source_vm_names=..., cloned_vms=...)`. Orphaned VMDKs left on the consumer clones are recorded for
reattachment during teardown so normal VM deletion removes them.

`create_plan_resource()` renames the per-VM snake_case `migrate_shared_disks` to the camelCase `migrateSharedDisks` API
field before serializing the VM list.

**Linux step pattern (6 steps).** `TestSharedDiskRhelMigration` (markers `vsphere`, `shared_disk`, `tier0`) inserts
`test_verify_shared_disk_data` before `test_check_vms`. That step calls `verify_shared_disk_data()`, which:

1. Locates the single shared PVC between the two destination VMs and maps each VM to its block device
2. VM1 mounts the shared partition, writes a marker file, syncs, unmounts
3. VM2 mounts it, reads VM1's marker, writes its own marker, syncs, unmounts
4. VM1 runs `blockdev --flushbufs`, remounts, and reads VM2's marker — the flush is required because XFS keeps metadata in
   the kernel buffer cache

**Windows step pattern (7 steps).** `TestSharedDiskWindowsMigration` (markers `vsphere`, `shared_disk`, `tier1`) prepends
`test_label_shared_disk`, which calls `label_shared_disk_on_source_windows()` before migration. It finds the shared VMDK's
backing UUID on the owner VM, maps it to the Windows disk serial, powers the owner VM on, and runs a PowerShell command
through VMware Guest Operations to write a session-unique NTFS volume label into `prepared_plan["_shared_disk_label"]`. It
also disables Fast Startup and shuts the owner VM down gracefully. Post-migration verification uses
`verify_shared_disk_data_windows()`, which reads the same label and performs the bidirectional read/write check with
drive-letter discovery and a targeted `Update-HostStorageCache` refresh.

> **Warning:** The shared disk must already carry a filesystem and be unmounted on the source VMs. virt-v2v cannot update
> `fstab` for shared disks, so `verify_shared_disk_data()` mounts the partition manually.

> **Note:** Shared-disk migration is vSphere-only. The fixture raises `ValueError` for any other provider type before
> cloning starts, and only one shared VMDK per plan is supported.

## ESXi host pinning and DRS

Three plan flags control where the **source clone** lives, which matters because copy-offload and throttling verification
compares per-host behaviour:

| Flag | Effect | Overridable |
| --- | --- | --- |
| `clone_to_same_host` | VM 2+ are pinned to the ESXi host used by VM 1 | Yes, by a per-VM `target_esxi_host` |
| `pin_to_non_dedicated_host` | Every VM is pinned to an ESXi host outside `copyoffload.dedicated_migration_hosts` | Yes, by a per-VM `target_esxi_host` |
| `disable_drs_for_vms` | After cloning, a per-VM DRS override pins the VM to its current ESXi host | No |

Both pinning flags use `setdefault`, so an explicit `target_esxi_host` on the VM entry wins. `clone_to_same_host` raises
`ValueError` when the first clone has no runtime host, because later clones cannot be pinned.

`pin_to_non_dedicated_host` resolves one host through `resolve_non_dedicated_esxi_host()` and reuses it for every VM, which
is what prevents a VM's own ESXi host from coinciding with a dedicated migration host and producing a false pass.

`disable_drs_for_vms` is required alongside `clone_to_same_host` so DRS cannot move a VM between hosts after cloning but
before migration. It raises `ValueError` for OVA providers and for any non-`VMWareProvider` clone provider.

Real configs, from the throttling and dedicated-host suites:

```python
"clone_to_same_host": True,
"disable_drs_for_vms": True,
"inventory_timeout": 600,
```

```python
"disable_drs_for_vms": True,
"pin_to_non_dedicated_host": True,
"inventory_timeout": 600,
```

## Skipping the clone phase

`skip_clone: True` makes `prepared_plan` use the existing source VMs instead of cloning them, which is how plan-readiness
and CA-certificate field tests avoid mutating shared infrastructure. It also captures and restores each VM's original power
state during teardown, because these are real, shared VMs rather than disposable clones.

> **Warning:** `skip_clone` is incompatible with `disable_drs_for_vms`, `clone_to_same_host`, `preserve_static_ips` on
> non-Hyper-V providers, `migrate_shared_disks`, and `add_nic` — all of those need the cloning phase. It also raises
> `ValueError` on providers whose `supports_skip_clone()` is `False`, which today means RHV.

## RDM as LUN

**Problem.** Raw Device Mapping disks default to virtio on the destination VM. Some guests need them presented as
SCSI LUN devices instead.

**Config keys.** `add_disks` entries carry `rdm_type: "virtual"` or `"physical"` on the source clone, and the plan-level
`rdm_as_lun: True` selects the LUN presentation:

```python
"test_copyoffload_rdm_virtual_disk_migration": {
    "virtual_machines": [
        {
            "name": "xcopy-template-test",
            "guest_agent": True,
            "clone": True,
            "add_disks": [
                {"rdm_type": "virtual"},  # LUN UUID from copyoffload.rdm_lun_uuid
            ],
        },
    ],
    "warm_migration": False,
    "copyoffload": True,
    "rdm_as_lun": True,
},
```

The physical-mode variant is identical apart from `rdm_type: "physical"`, which uses `physicalMode` with
`independent_persistent`. The LUN UUID comes from `copyoffload.rdm_lun_uuid` in `.providers.json`, which the schema
constrains to the NAA format (`^naa\.`).

`test_copyoffload_warm_rdm_virtual_disk_migration` adds no `rdm_as_lun`, so it covers the default virtio presentation.
The three classes are `TestCopyoffloadRdmVirtualDiskMigration`, `TestCopyoffloadRdmPhysicalDiskMigration`, and
`TestCopyoffloadWarmRdmVirtualDiskMigration`.

> **Note:** `rdm_as_lun` applies only to vSphere source providers.

## Plan archive and delete: PVC and DataVolume cleanup

**Problem.** When a migration fails, the Plan and Migration CRs stay behind along with any DataVolumes and PVCs that were
already created. Archiving and deleting the Plan must clean them up, or the target namespace leaks storage.

**Config keys.** A normal plan plus a failing post-hook and an isolated VM namespace:

```python
"test_plan_archive_pvc_cleanup": {
    "virtual_machines": [
        {"name": "mtv-tests-rhel8", "source_vm_power": "on", "guest_agent": True},
    ],
    "warm_migration": False,
    "target_power_state": "off",
    "post_hook": {"expected_result": "fail"},
    "vm_target_namespace": f"mtv-vms-archive-{uuid.uuid4().hex[:4]}",
},
```

**Step pattern (6 steps).** storagemap → networkmap → plan → migrate (expected failure) → `test_archive_and_delete_plan` →
`test_verify_pvc_cleanup`. There is no `test_check_vms`.

- `test_migrate_vms` — wraps `execute_migration()` in `pytest.raises(MigrationPlanExecError)`, then calls
  `validate_hook_failure_and_check_vms()`, which asserts every VM failed at the same step and that it matches the
  configured hook.
- `test_archive_and_delete_plan` — asserts at least one PVC or DataVolume exists in the VM namespace (vacuity guard),
  calls `archive_plan()`, waits for the `Archived` condition, deletes the Plan, unregisters Plan and Migration from
  teardown, then waits up to 120s for the Migration to be cascade-deleted.
- `test_verify_pvc_cleanup` — deletes the retained destination VM first, then polls `get_orphan_resource_names()` every
  5s for up to 120s until no `PVC/` or `DV/` remains.

`TestPlanArchivePvcCleanup` carries `@pytest.mark.vsphere`, `rhv`, `openstack`, `openshift`, `tier1`, and `incremental`.

> **Warning:** `test_verify_pvc_cleanup` must delete the destination VM before polling. That VM owns a DataVolume and a
> PVC, so an unfiltered namespace poll would never empty and the step would always time out.

> **Note:** `archive_plan()` also deletes plan-owned populator pods in the target namespace and logs an error for any pod
> that does not disappear. This test complements the interrupted-transfer prime-PVC scenario but does not reproduce it,
> because a prime PVC may already be gone by the time a PostHook failure is raised.

## Configuration validation errors

The preparation and plan helpers fail fast rather than silently degrading. The checks you are most likely to hit:

| Trigger | Error |
| --- | --- |
| `add_nic: True` without `add_nic_start_connected`, or with a non-bool value | `ValueError` naming the VM |
| `add_nic: True` on a non-vSphere provider | `pytest.skip` |
| Shared-disk config on a provider without `relink_shared_disks()` | `ValueError` naming the provider |
| `disable_drs_for_vms` on OVA or a non-VMware clone provider | `ValueError` |
| `skip_clone` combined with an incompatible flag, or on RHV | `ValueError` listing the conflicts |
| `preserve_static_ips` without `source_vm_power: "on"` on some VM | `ValueError` naming the VM |
| `clone_to_same_host` when the first clone has no runtime host | `ValueError` naming the VM |
| LUKS config with no resolvable passphrase | `ValueError` naming the VM and the resolution source |
| `pvc_name_template` map with a key that is not a provider type or `default` | `ValueError` listing the allowed keys |
| `target_labels` missing from the test config | `ValueError` naming `target_labels` |
| `target_node_selector` with anything other than exactly one label | `ValueError` reporting the count |
