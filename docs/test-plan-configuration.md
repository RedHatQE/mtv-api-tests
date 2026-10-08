# Test Plan Configuration

`tests_params` is the catalog of named migration plans used by this repository. Each entry defines which source VMs to use, which MTV plan features to enable, and which extra
test behaviors should run before or after migration.

The important thing to know is that the value you write in `tests/tests_config/config.py` is the **raw plan**. The test fixtures then turn that raw plan into a runtime
`prepared_plan` before creating the MTV `Plan` custom resource.

## Where Plans Live

The suite loads `tests/tests_config/config.py` as a Python config file, not as YAML or JSON:

```4:9:pytest.ini
addopts =
  -s
  -o log_cli=true
  -p no:logging
  --tc-file=tests/tests_config/config.py
  --tc-format=python
```

Each test class then picks one named entry from `py_config["tests_params"]`:

```27:36:tests/cold/test_mtv_cold_migration.py
@pytest.mark.parametrize(
    "class_plan_config",
    [
        pytest.param(
            py_config["tests_params"]["test_sanity_cold_mtv_migration"],
        )
    ],
    indirect=True,
    ids=["rhel8"],
)
```

> **Note:** `tests/tests_config/config.py` contains both shared test-suite settings and the `tests_params` dictionary. Only entries inside `tests_params` are individual plan
> definitions.

## Basic Structure

A minimal plan can be very small:

```49:55:tests/tests_config/config.py
    "test_sanity_cold_mtv_migration": {
        "virtual_machines": [
            {"name": "mtv-tests-rhel8", "guest_agent": True, "add_nic": True, "add_nic_start_connected": False},
        ],
        "warm_migration": False,
        "per_nic_network_map": True,
    },
```

A more advanced plan adds destination placement, PVC naming, labels, and affinity:

```665:704:tests/tests_config/config.py
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

In every entry:

- `virtual_machines` is the per-VM section.
- Everything else is plan-level behavior or test behavior.

> **Note:** This file is plain Python. Use Python values such as `True`, `False`, and `None`, not JSON values like `true`, `false`, or `null`.

## Per-VM Settings

Each item in `virtual_machines` describes one source VM or template.

| Key | Required | Values or meaning |
| --- | --- | --- |
| `name` | Yes | Source VM or template name. During preparation this may be rewritten to the actual cloned runtime name. |
| `source_vm_power` | No | `"on"` starts the VM, `"off"` stops it. Omitting it leaves the current state unchanged. |
| `guest_agent` | No | `True` when the guest agent is expected to run after migration. |
| `clone` | No | `True` marks the VM as a placeholder to migrate: on copy-offload plans the name is replaced by `copyoffload.default_vm_name`. It does **not** switch the cloning phase off — `prepared_plan` clones every VM unless the plan sets `skip_clone: True`. |
| `clone_name` | No | Overrides the default clone base name. |
| `preserve_name_format` | No | `True` keeps uppercase letters and underscores in `clone_name` instead of sanitizing them. |
| `disk_type` | No | `thin`, `thick-lazy`, or `thick-eager`. |
| `add_disks` | No | List of extra disks to attach to the cloned VM before migration. |
| `snapshots` | No | Number of source snapshots to create before migration. |
| `target_datastore_id` | No | VMware target datastore for the cloned VM. |
| `target_esxi_host` | No | Pin the clone to a specific ESXi host. Set automatically by `clone_to_same_host` and `pin_to_non_dedicated_host` unless you set it yourself. |
| `luks` | No | `True` when the VM has a LUKS-encrypted disk. Consumed by the LUKS fixture and replaced with a Secret reference. |
| `luks_passphrase` | No | Per-VM LUKS passphrase. Overrides the provider-level `luks_passphrase`. |
| `migrate_shared_disks` | No | `True` for the owner VM in a shared-disk pair. Overrides the plan-level value for that VM. |
| `add_nic` | No | `True` to add an extra NIC. vSphere only. |
| `add_nic_start_connected` | Yes* | Required when `add_nic` is `True`. Must be a `bool`. |
| `win_os` | No | Hyper-V guest OS override. Must be a `bool` when present. |

Validation is strict for the rows marked with an asterisk:

- `add_nic: True` without `add_nic_start_connected` raises a `ValueError`.
- A non-`bool` `add_nic_start_connected` raises a `ValueError`.
- `add_nic` on a non-vSphere provider skips the test.
- `win_os` with a non-`bool` value raises a `TypeError` in the Hyper-V provider.

### Disk types

```80:84:libs/providers/vmware.py
    DISK_TYPE_MAP = {
        "thin": ("sparse", "Setting disk provisioning to 'thin' (sparse)."),
        "thick-lazy": ("flat", "Setting disk provisioning to 'thick-lazy' (flat)."),
        "thick-eager": ("flat", "Setting disk provisioning to 'thick-eager' (flat + eagerlyScrub)."),
    }
```

`thick-eager` maps to a flat disk with `eagerlyScrub` set, not to `eagerZeroedThick`.

### `add_disks` entries

Each item in `add_disks` supports these keys:

| Key | Required | Meaning |
| --- | --- | --- |
| `size_gb` | Yes for a normal added disk | Disk size in gigabytes. |
| `provision_type` | No | `thin`, `thick-lazy`, or `thick-eager`. Defaults to `thin`. |
| `disk_mode` | No | vSphere disk mode. Defaults to `persistent`. `independent_persistent` is used in `tests_params`. |
| `datastore_id` | No | Places this disk on a specific datastore instead of the VM's default. |
| `datastore_path` | No | Directory path to create on the datastore, for datastore-path tests. |
| `rdm_type` | No | `virtual` or `physical`. Creates an RDM disk from `copyoffload.rdm_lun_uuid`. |

A real multi-disk example:

```148:161:tests/tests_config/config.py
    "test_copyoffload_multi_disk_migration": {
        "virtual_machines": [
            {
                "name": "xcopy-template-test",
                "guest_agent": True,
                "clone": True,
                "add_disks": [
                    {"size_gb": 30, "disk_mode": "persistent", "provision_type": "thick-lazy"},
                ],
            },
        ],
        "warm_migration": False,
        "copyoffload": True,
    },
```

For special naming tests, the config can override the clone name entirely:

```549:562:tests/tests_config/config.py
    "test_copyoffload_nonconforming_name_migration": {
        "virtual_machines": [
            {
                "name": "xcopy-template-test",
                "clone_name": "XCopy_Test_VM_CAPS",  # Non-conforming name for cloned VM
                "preserve_name_format": True,  # Don't sanitize the name (keep capitals and underscores)
                "guest_agent": True,
                "clone": True,
                "disk_type": "thin",
            },
        ],
        "warm_migration": False,
        "copyoffload": True,
    },
```

### Snapshot-Driven Tests

`snapshots` affects **test preparation**, not just MTV plan creation. Snapshot tests create the snapshots first, then store the pre-migration snapshot list back into the prepared
plan for later validation:

```271:292:tests/copyoffload/test_copyoffload_migration.py
        vm_cfg = prepared_plan["virtual_machines"][0]
        provider_vm_api = prepared_plan["source_vms_data"][vm_cfg["name"]]["provider_vm_api"]

        source_provider.start_vm(provider_vm_api)
        source_provider.wait_for_vmware_guest_info(provider_vm_api, timeout=60)

        snapshots_to_create = int(vm_cfg["snapshots"])
        snapshot_prefix = f"{vm_cfg['name']}-{fixture_store['session_uuid']}-snapshot"

        for idx in range(1, snapshots_to_create + 1):
            source_provider.create_snapshot(
                vm=provider_vm_api,
                name=f"{snapshot_prefix}-{idx}",
                description="mtv-api-tests copy-offload snapshots migration test",
                memory=False,
                quiesce=False,
                wait_timeout=60 * 10,
            )

        vm_cfg["snapshots_before_migration"] = source_provider.vm_dict(provider_vm_api=provider_vm_api)[
            "snapshots_data"
        ]
```

> **Note:** In the class-based flow used by this repository, external-provider VMs are resolved with `clone_vm=True` during preparation. That means runtime VM names usually
> become per-test clone names even when the raw plan entry does not set `clone: true`.

## Plan-Level Settings

### Migration behavior

| Key | Required | Values or meaning |
| --- | --- | --- |
| `warm_migration` | No | `True` for warm migration. Also adds the CTK flag to the clone options. |
| `copyoffload` | No | `True` to enable copy-offload (XCOPY) migration. |
| `target_power_state` | No | `on` or `off`. Expected power state of the migrated VM after completion. |
| `preserve_static_ips` | No | `True` to preserve static IP addresses. Requires `source_vm_power: "on"` for every VM. |
| `guest_agent_timeout` | No | Guest-info wait timeout in seconds. Defaults to `120` where used. |
| `migrate_shared_disks` | No | `True` to enable shared disk migration at plan level. Individual VMs can override. |
| `xfs_compatibility` | No | `True` to request XFS v4 filesystem compatibility on the Plan CR. |
| `rdm_as_lun` | No | `True` to map RDM disks as LUN devices with a SCSI bus instead of the default virtio. |
| `enable_nested_virtualization` | No | `False` disables the `vmx` and `svm` CPU features. |
| `run_preflight_inspection` | No | `False` disables the Plan CR preflight inspection step. |

`preserve_static_ips` is validated up front. Every VM must declare `source_vm_power: "on"`, because guest tools must be running to collect static IP and NIC name data.

If you omit `target_power_state`, post-migration validation falls back to the source VM power state:

```1287:1307:utilities/post_migration.py
def check_vms_power_state(
    source_vm: dict[str, Any],
    destination_vm: dict[str, Any],
    source_power_before_migration: str | None,
    target_power_state: str | None = None,
) -> None:
    # If targetPowerState is specified, check that the destination VM matches it
    if target_power_state:
        actual_power_state = destination_vm["power_state"]
        LOGGER.info(f"Checking target power state: expected={target_power_state}, actual={actual_power_state}")
        assert actual_power_state == target_power_state, (
            f"VM power state mismatch: expected {target_power_state}, got {actual_power_state}"
        )
        LOGGER.info(f"Target power state verification passed: {actual_power_state}")
    elif source_power_before_migration:
        if source_power_before_migration not in ("on", "off"):
            raise ValueError(f"Invalid source_vm_power '{source_power_before_migration}'. Must be 'on' or 'off'")
        # Default behavior: destination VM should match source power state before migration
        assert destination_vm["power_state"] == source_power_before_migration
```

### Placement, naming, and scheduling

| Key | Required | Meaning |
| --- | --- | --- |
| `vm_target_namespace` | No | Custom namespace for migrated VMs. The preparation flow creates it if needed. |
| `multus_namespace` | No | Namespace where NetworkAttachmentDefinitions are created for additional networks. |
| `pvc_name_template` | No | Forklift PVC naming template. Either a string, or a mapping of provider type to template. |
| `pvc_name_template_use_generate_name` | No | When `True`, post-checks treat the rendered PVC name as a prefix because Kubernetes adds a generated suffix. |
| `target_node_selector` | No | Node label selector used for VM placement tests. |
| `target_labels` | No | Labels to apply to migrated VM metadata. |
| `target_affinity` | No | Affinity rules passed to the migrated VM template. |
| `inventory_timeout` | No | Seconds to wait for cloned VMs in the Forklift inventory. Defaults to `300`. |

`None` has a special meaning in `target_labels` and `target_node_selector`: the fixtures replace it with the current `session_uuid` so each test run gets a unique value.

`pvc_name_template` accepts two shapes. A plain string is used as is. A mapping must use `Provider.ProviderType` values, such as `vsphere` and `ovirt`, plus the optional key
`default`. Unknown keys raise a `ValueError`, and a mapping with neither a matching provider key nor `default` also raises. The mapping is resolved during preparation, before the
Plan CR is created.

> **Tip:** `vm_target_namespace` and `multus_namespace` solve different problems. Use `vm_target_namespace` to choose where migrated VMs land, and `multus_namespace` to choose
> where the test-created NetworkAttachmentDefinitions live.

### Clone placement and throttling

| Key | Required | Meaning |
| --- | --- | --- |
| `clone_to_same_host` | No | `True` pins VM2 and later to the ESXi host of the first VM. An explicit `target_esxi_host` wins. |
| `pin_to_non_dedicated_host` | No | `True` pins every VM to an ESXi host outside `copyoffload.dedicated_migration_hosts`. An explicit `target_esxi_host` wins. |
| `disable_drs_for_vms` | No | `True` disables vSphere DRS per VM after cloning. Not supported for OVA, and it requires a VMware clone provider. |
| `skip_clone` | No | `True` skips the clone phase and uses the existing VMs directly. |
| `per_nic_network_map` | No | `True` creates per-NIC network mappings, which allows duplicate source network entries in the NetworkMap. |

`pin_to_non_dedicated_host` needs `copyoffload.dedicated_migration_hosts` in the provider entry. Without it, the code raises a `ValueError`.

`skip_clone` has its own validation. It is rejected when the provider does not support skipping clones, and it is incompatible with `disable_drs_for_vms`, `clone_to_same_host`,
`migrate_shared_disks`, and `add_nic`, plus `preserve_static_ips` on every provider except Hyper-V. Because skipped VMs are real and shared, the fixture restores their original
power states during teardown.

### XFS verification

XFS tests need both `xfs_compatibility: True` and an `xfs_check` block:

```854:870:tests/tests_config/config.py
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

| Key | Required | Meaning |
| --- | --- | --- |
| `xfs_check.command` | Yes | Command to run on the migrated guest. |
| `xfs_check.mount_point` | Yes | Argument passed to the command, typically the mount point. |
| `xfs_check.expected_output` | Yes | String that must appear in the command output. |

The verification step runs `[command, mount_point]` over SSH and asserts that `expected_output` is in stdout. It is Linux-only and raises a `ValueError` for a Windows guest.

### Hooks and failure expectations

The repository also supports hook-driven tests. A simple failure-path example looks like this:

```743:756:tests/tests_config/config.py
    "test_post_hook_retain_failed_vm": {
        "virtual_machines": [
            {
                "name": "mtv-tests-rhel8",
                "source_vm_power": "on",
                "guest_agent": True,
            },
        ],
        "warm_migration": False,
        "target_power_state": "off",
        "pre_hook": {"expected_result": "succeed"},
        "post_hook": {"expected_result": "fail"},
        "expected_migration_result": "fail",
    },
```

In hook config:

| Key | Meaning |
| --- | --- |
| `pre_hook` / `post_hook` | Dictionaries describing a pre-migration or post-migration Hook CR. |
| `expected_migration_result` | Tells the test whether a migration failure is expected. |

A hook dictionary must contain exactly one of these keys:

| Key | Values |
| --- | --- |
| `expected_result` | `succeed` or `fail`. Selects a predefined playbook. |
| `playbook_base64` | A base64-encoded Ansible playbook, validated for base64, UTF-8, and YAML syntax. |
| `aap_job_template_id` | A positive integer AWX job template ID for an AAP hook. |

Specifying more than one, or none of them, raises a `ValueError`. `expected_result` and `playbook_base64` cannot be empty or whitespace-only.

## Copy-Offload-Specific Notes

`copyoffload: true` is only the **plan-side** switch. The source provider also needs a `copyoffload` section in `.providers.json`.

> **Warning:** A raw plan with `copyoffload: true` is not enough by itself. The test session validates that the source provider is vSphere, that the `copyoffload` section
> exists, that `storage_hostname`, `storage_username`, and `storage_password` resolve, and that `storage_vendor_product` and `datastore_id` are set.

A plan that drives dedicated-host verification looks like this:

```416:444:tests/tests_config/config.py
    "test_copyoffload_dedicated_migration_host_migration": {
        "virtual_machines": [
            {
                "name": "xcopy-template-test",
                "guest_agent": True,
                "clone": True,
                "disk_type": "thin",
                "add_disks": [
                    {"size_gb": 10, "disk_mode": "persistent", "provision_type": "thin"},
                    {"size_gb": 10, "disk_mode": "persistent", "provision_type": "thin"},
                ],
            },
            {
                "name": "xcopy-template-test",
                "guest_agent": True,
                "clone": True,
                "disk_type": "thin",
                "add_disks": [
                    {"size_gb": 10, "disk_mode": "persistent", "provision_type": "thin"},
                    {"size_gb": 10, "disk_mode": "persistent", "provision_type": "thin"},
                ],
            },
        ],
        "warm_migration": False,
        "copyoffload": True,
        "disable_drs_for_vms": True,
        "pin_to_non_dedicated_host": True,
        "inventory_timeout": 600,
    },
```

There is one more important implementation detail: when the helper creates an MTV plan with `copyoffload=True`, it forces `pvc_name_template` to `"pvc"`.

```328:333:utilities/mtv_migration.py
    # Add copy-offload specific parameters if enabled
    if copyoffload:
        # Set PVC naming template for copy-offload migrations
        # The volume populator framework requires this to generate consistent PVC names
        # Note: generateName is enabled by default, so Kubernetes adds random suffix automatically
        plan_kwargs["pvc_name_template"] = "pvc"
```

> **Warning:** Do not expect a custom `pvc_name_template` in `tests_params` to survive copy-offload plan creation. The helper overwrites it with `"pvc"`.

Provider-side copy-offload settings are documented in [Provider Config File](provider-config-file.md#vsphere-copy-offload).

## How Raw Config Becomes `prepared_plan`

The raw entry from `tests_params` is not used directly. The repository converts it into a runtime `prepared_plan` in several steps.

### 1. The Raw Plan Is Deep-Copied

The fixture copies the selected config, resolves a mapping-form `pvc_name_template`, and sets up runtime-only storage:

```1156:1182:conftest.py
    # Deep copy the plan config to avoid mutation
    plan: dict[str, Any] = deepcopy(class_plan_config)

    if "pvc_name_template" in plan:
        plan["pvc_name_template"] = resolve_pvc_name_template(
            pvc_name_template=plan["pvc_name_template"],
            source_provider_type=source_provider.type,
        )

    virtual_machines: list[dict[str, Any]] = plan["virtual_machines"]
    warm_migration = plan.get("warm_migration", False)

    # Initialize separate storage for source VM data (keeps virtual_machines clean for Plan CR serialization)
    plan["source_vms_data"] = {}

    # Handle custom VM target namespace
    vm_target_namespace = plan.get("vm_target_namespace")
    if vm_target_namespace:
        LOGGER.info(f"Using custom VM target namespace: {vm_target_namespace}")
        get_or_create_namespace(
            fixture_store=fixture_store,
            ocp_admin_client=ocp_admin_client,
            namespace_name=vm_target_namespace,
        )
        plan["_vm_target_namespace"] = vm_target_namespace
    else:
        plan["_vm_target_namespace"] = target_namespace
```

### 2. The Clone Phase Runs

Unless `skip_clone` is set, each VM is cloned, the plan-level placement flags are applied, and shared disks are relinked between clones:

```1278:1297:conftest.py
        skip_clone = plan.get("skip_clone", False)

        if skip_clone:
            if not source_provider.supports_skip_clone():
                raise ValueError(
                    f"skip_clone=True is not supported for provider type '{source_provider.type}'; "
                    "VMs must be cloned from templates."
                )
            skip_clone_incompatible = ["disable_drs_for_vms", "clone_to_same_host"]
            if source_provider.type != Provider.ProviderType.HYPERV:
                skip_clone_incompatible.append("preserve_static_ips")
            conflicting = [flag for flag in skip_clone_incompatible if plan.get(flag)]
            if has_shared_disk_config:
                conflicting.append("migrate_shared_disks")
            if has_add_nic_config:
                conflicting.append("add_nic")
            if conflicting:
                raise ValueError(
                    f"skip_clone=True is incompatible with {conflicting}; these options require the cloning phase."
                )
```

### 3. Each VM Is Cloned and Renamed

In the clone phase, the fixture resolves each VM through the clone provider, applies the placement flags, sets the source power state, and rewrites the VM name to the actual
runtime name:

```1331:1356:conftest.py
        if not skip_clone:
            for vm in virtual_machines:
                clone_options = {**vm, "enable_ctk": warm_migration}

                # Pin VM2+ to same ESXi host as VM1 (required for per-host inflight throttling).
                # Uses setdefault to respect any explicit per-VM target_esxi_host override.
                if plan.get("clone_to_same_host", False) and first_vm_esxi_host:
                    clone_options.setdefault("target_esxi_host", first_vm_esxi_host)

                # Pin every VM to an ESXi host outside the configured dedicated hosts, so
                # dedicated-host verification can never coincide with a VM's own host (MTV-6136).
                if plan.get("pin_to_non_dedicated_host", False):
                    if non_dedicated_host_name is None:
                        non_dedicated_host_name = resolve_non_dedicated_esxi_host(
                            source_provider_inventory=source_provider_inventory,
                            source_provider_data=source_provider_data,
                        )
                    clone_options.setdefault("target_esxi_host", non_dedicated_host_name)

                provider_vm_api = clone_provider.get_vm_by_name(
                    query=vm["name"],
                    vm_name_suffix=vm_name_suffix,
                    clone_vm=True,
                    session_uuid=fixture_store["session_uuid"],
                    clone_options=clone_options,
                )
```

### 4. Hooks Are Created

After the VM loop, configured hooks become real `Hook` custom resources:

```1497:1498:conftest.py
    create_hook_if_configured(plan, "pre_hook", "pre", fixture_store, ocp_admin_client, target_namespace)
    create_hook_if_configured(plan, "post_hook", "post", fixture_store, ocp_admin_client, target_namespace)
```

### 5. The Test Adds VM IDs and Creates the MTV Plan

Right before creating the MTV `Plan`, the tests add VM IDs from the Forklift inventory and pass the prepared values into `create_plan_resource()`:

```175:193:tests/warm/test_warm_migration_comprehensive.py
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

`create_plan_resource()` accepts only the flags that map onto the Plan CR: `warm_migration`, `copyoffload`, `preserve_static_ips`, `pvc_name_template`,
`pvc_name_template_use_generate_name`, `target_node_selector`, `target_labels`, `target_affinity`, `vm_target_namespace`, `migrate_shared_disks`, `target_power_state`,
`enable_nested_virtualization`, `xfs_compatibility`, `run_preflight_inspection`, and `rdm_as_lun`. Everything else is consumed by fixtures or by post-migration checks.

## Runtime Fields Added During Preparation

These fields are derived at runtime. You do **not** write them yourself in `tests_params`.

| Field | Added by | Purpose |
| --- | --- | --- |
| `source_vms_data` | `prepared_plan` | Stores rich source VM details for later validation without polluting `virtual_machines`. |
| `_vm_target_namespace` | `prepared_plan` | Resolved namespace used by post-migration validation. |
| `_pre_hook_name` / `_pre_hook_namespace` | Hook creation | Created Hook CR reference for plan creation. |
| `_post_hook_name` / `_post_hook_namespace` | Hook creation | Created Hook CR reference for plan creation. |
| `virtual_machines[*].name` | `prepared_plan` | Rewritten to the actual runtime VM name after provider lookup or cloning. |
| `virtual_machines[*].targetName` | `prepared_plan` | Set for OVA only, as a per-session name so parallel runs do not collide. |
| `virtual_machines[*].snapshots_before_migration` | `prepared_plan` or snapshot test setup | Snapshot baseline used for post-migration checks. |
| `virtual_machines[*].connected_nic_mac` / `disconnected_nic_mac` | `add_nic` handling | MAC of the NIC added for the test. |
| `virtual_machines[*].luks` | LUKS fixture | Replaced by a `{"name": ...}` Secret reference. |
| `virtual_machines[*].id` | `populate_vm_ids()` | Forklift VM ID required by plan creation. |

A few raw config fields are resolved by companion fixtures instead of `prepared_plan` itself:

- `multus_namespace` is consumed by the network setup fixture that creates NADs.
- `target_labels` is resolved by `target_vm_labels`.
- `target_node_selector` is resolved by `labeled_worker_node`.

> **Tip:** Keep `tests_params` declarative. Do not hand-write runtime fields such as `id`, `source_vms_data`, `_vm_target_namespace`, or `snapshots_before_migration`. Let the
> fixtures derive them.

## Practical Authoring Guidelines

- Start with one VM and one or two plan-level flags, then add more only when the scenario really needs them.
- Treat `virtual_machines` as source-side intent and `prepared_plan` as runtime state. They are not the same thing.
- Use `None` intentionally in `target_labels` or `target_node_selector` when you want a unique value per run.
- Expect VM names in `prepared_plan["virtual_machines"]` to differ from the raw `name` once preparation is complete.
- For copy-offload scenarios, think in two layers: test plan config in `tests_params`, and provider/storage config in `.providers.json`.
- Copy the key names exactly. Required keys are read directly, so a typo there is a `KeyError`; optional flags are read with
  `.get()`, so a misspelled flag such as `skip_clone` or `clone_to_same_host` silently takes its default and changes the scenario instead of failing.
