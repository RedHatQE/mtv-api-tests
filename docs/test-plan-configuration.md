# Test Plan Configuration

`tests_params` is the catalog of named migration plans used by this repository. Each entry defines which source VMs to use, which MTV plan features to enable, and which extra test behaviors should run before or after migration.

The important thing to know is that the value you write in `tests/tests_config/config.py` is the **raw plan**. The test fixtures then turn that raw plan into a runtime `prepared_plan` before creating the MTV `Plan` custom resource.

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

```19:29:tests/test_mtv_cold_migration.py
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

> **Note:** `tests/tests_config/config.py` contains both shared test-suite settings and the `tests_params` dictionary. Only entries inside `tests_params` are individual plan definitions.

## Basic Structure

A minimal plan can be very small:

```46:50:tests/tests_config/config.py
"test_sanity_cold_mtv_migration": {
    "virtual_machines": [
        {"name": "mtv-tests-rhel8", "guest_agent": True},
    ],
    "warm_migration": False,
},
```

A more advanced plan adds destination placement, PVC naming, labels, and affinity:

```434:466:tests/tests_config/config.py
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
    "vm_target_namespace": "custom-vm-namespace",
    "multus_namespace": "default",  # Cross-namespace NAD access
    "pvc_name_template": '{{ .FileName | trimSuffix ".vmdk" | replace "_" "-" }}-{{.DiskIndex}}',
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
},
```

In every entry:

- `virtual_machines` is the per-VM section.
- Everything else is plan-level behavior or test behavior.

> **Note:** This file is plain Python. Use Python values such as `True`, `False`, and `None`, not JSON values like `true`, `false`, or `null`.

## Per-VM Settings

Each item in `virtual_machines` describes one source VM or template.

| Key | What it means |
| --- | --- |
| `name` | Source VM or template name. During preparation, this may be rewritten to the actual cloned runtime name. |
| `source_vm_power` | Optional source power state before migration. `"on"` starts the VM, `"off"` stops it, and omitting it leaves the current state unchanged. |
| `guest_agent` | Enables guest-agent-aware validation after migration. |
| `clone` | Common in copy-offload and mutation-heavy scenarios. In this repository it is mostly a scenario hint, not the only thing that causes runtime cloning. |
| `clone_name` | Overrides the default clone base name. |
| `preserve_name_format` | Preserves uppercase letters and underscores in `clone_name` for name-format tests. |
| `disk_type` | VMware disk provisioning mode for the cloned VM. |
| `add_disks` | Adds extra disks to the cloned source VM before migration. |
| `snapshots` | Number of source snapshots to create before migration in snapshot-focused tests. |
| `target_datastore_id` | VMware target datastore for the cloned VM. |
| `add_disks[*].datastore_id` | Optional datastore override for an individual added disk. |

The VMware provider currently supports these `disk_type` values:

```77:80:libs/providers/vmware.py
DISK_TYPE_MAP = {
    "thin": ("sparse", "Setting disk provisioning to 'thin' (sparse)."),
    "thick-lazy": ("flat", "Setting disk provisioning to 'thick-lazy' (flat)."),
    "thick-eager": ("eagerZeroedThick", "Setting disk provisioning to 'thick-eager' (eagerZeroedThick)."),
}
```

A real multi-disk example looks like this:

```87:100:tests/tests_config/config.py
"test_copyoffload_multi_disk_migration": {
    "virtual_machines": [
        {
            "name": "xcopy-template-test",
            "source_vm_power": "off",
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

```331:345:tests/tests_config/config.py
"test_copyoffload_nonconforming_name_migration": {
    "virtual_machines": [
        {
            "name": "xcopy-template-test",
            "clone_name": "XCopy_Test_VM_CAPS",  # Non-conforming name for cloned VM
            "preserve_name_format": True,  # Don't sanitize the name (keep capitals and underscores)
            "source_vm_power": "off",
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

`snapshots` is a good example of a value that affects **test preparation**, not just MTV plan creation. Snapshot tests create the snapshots first, then store the pre-migration snapshot list back into the prepared plan for later validation:

```223:248:tests/test_copyoffload_migration.py
vm_cfg = prepared_plan["virtual_machines"][0]
provider_vm_api = prepared_plan["source_vms_data"][vm_cfg["name"]]["provider_vm_api"]

# Ensure VM is powered on for snapshot creation
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

# Refresh and store snapshots list for post-migration snapshot checks
vm_cfg["snapshots_before_migration"] = source_provider.vm_dict(provider_vm_api=provider_vm_api)[
    "snapshots_data"
]
```

> **Note:** In the class-based flow used by this repository, external-provider VMs are resolved with `clone_vm=True` during preparation. That means runtime VM names usually become per-test clone names even when the raw plan entry does not set `clone: true`.

## Plan-Level Settings

### Migration Behavior

- `warm_migration`: Switches between warm and cold migration behavior.
- `copyoffload`: Enables copy-offload-specific plan handling and validation.
- `target_power_state`: Expected power state of the migrated VM after completion.
- `preserve_static_ips`: Passes the static-IP preservation setting into the MTV plan and enables related validation in supported scenarios.
- `guest_agent_timeout`: Overrides how long validation waits for the destination guest agent.

If you omit `target_power_state`, post-migration validation falls back to the source VM power state:

```799:818:utilities/post_migration.py
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
    elif source_power_before_migration:
        if source_power_before_migration not in ("on", "off"):
            raise ValueError(f"Invalid source_vm_power '{source_power_before_migration}'. Must be 'on' or 'off'")
        # Default behavior: destination VM should match source power state before migration
        assert destination_vm["power_state"] == source_power_before_migration
```

### Placement, Naming, and Scheduling

- `vm_target_namespace`: Custom namespace for migrated VMs. The preparation flow creates it if needed.
- `multus_namespace`: Namespace where NADs are created for additional networks.
- `pvc_name_template`: Forklift PVC naming template.
- `pvc_name_template_use_generate_name`: When `True`, post-checks treat the rendered PVC name as a prefix because Kubernetes adds a generated suffix.
- `target_node_selector`: Node label selector used for VM placement tests.
- `target_labels`: Labels to apply to migrated VM metadata.
- `target_affinity`: Affinity rules passed to the migrated VM template.

`None` has a special meaning in `target_labels` and `target_node_selector`: the fixtures replace it with the current `session_uuid` so each test run gets a unique value.

> **Tip:** `vm_target_namespace` and `multus_namespace` solve different problems. Use `vm_target_namespace` to choose where migrated VMs land, and `multus_namespace` to choose where the test-created NetworkAttachmentDefinitions live.

### Hooks and Failure Expectations

The repository also supports hook-driven tests. A simple failure-path example looks like this:

```503:515:tests/tests_config/config.py
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

- `pre_hook` and `post_hook` are dictionaries.
- The code accepts either `expected_result` (`"succeed"` or `"fail"`) or `playbook_base64` for a custom encoded Ansible playbook.
- `expected_migration_result` tells the test whether a migration failure is expected.

## Copy-Offload-Specific Notes

`copyoffload: true` is only the **plan-side** switch. The source provider also needs a `copyoffload` section in `.providers.json`.

A real provider-side example from `.providers.json.example` looks like this:

```27:58:.providers.json.example
"copyoffload": {
  # Supported storage_vendor_product values:
  # - "ontap"           (NetApp ONTAP)
  # - "vantara"         (Hitachi Vantara)
  # - "primera3par"     (HPE Primera/3PAR)
  # - "pureFlashArray"  (Pure Storage FlashArray)
  # - "powerflex"       (Dell PowerFlex)
  # - "powermax"        (Dell PowerMax)
  # - "powerstore"      (Dell PowerStore)
  # - "infinibox"       (Infinidat InfiniBox)
  # - "flashsystem"     (IBM FlashSystem)
  "storage_vendor_product": "ontap",

  # Primary datastore for copy-offload operations (required)
  # This is the vSphere datastore ID (e.g., "datastore-12345") where VMs reside
  # Get via vSphere: Datacenter → Storage → Datastore → Summary → More Objects ID
  "datastore_id": "datastore-12345",

  # Optional: Secondary datastore for multi-datastore copy-offload tests
  # Only needed when testing VMs with disks spanning multiple datastores
  # When specified, tests can validate copy-offload with disks on different datastores
  "secondary_datastore_id": "datastore-67890",

  # Optional: Non-XCOPY datastore for mixed datastore tests
  # This should be a datastore that does NOT support XCOPY/VAAI primitives
  # Used for testing VMs with disks on both XCOPY and non-XCOPY datastores
  "non_xcopy_datastore_id": "datastore-99999",

  "default_vm_name": "rhel9-template",
  "storage_hostname": "storage.example.com",
  "storage_username": "admin",
  "storage_password": "your-password-here",  # pragma: allowlist secret
```

> **Warning:** A raw plan with `copyoffload: true` is not enough by itself. The test session validates that the source provider is vSphere and that provider-side copy-offload settings are present, including at least `storage_vendor_product` and `datastore_id`.

There is one more important implementation detail: when the helper creates an MTV plan with `copyoffload=True`, it forces `pvc_name_template` to `"pvc"`.

```238:243:utilities/mtv_migration.py
# Add copy-offload specific parameters if enabled
if copyoffload:
    # Set PVC naming template for copy-offload migrations
    # The volume populator framework requires this to generate consistent PVC names
    # Note: generateName is enabled by default, so Kubernetes adds random suffix automatically
    plan_kwargs["pvc_name_template"] = "pvc"
```

> **Warning:** Do not expect a custom `pvc_name_template` in `tests_params` to survive copy-offload plan creation. The helper currently overwrites it with `"pvc"`.

## How Raw Config Becomes `prepared_plan`

The raw entry from `tests_params` is not used directly. The repository converts it into a runtime `prepared_plan` in several steps.

### 1. The Raw Plan Is Deep-Copied

The fixture starts by copying the selected config and setting up runtime-only storage:

```840:859:conftest.py
# Deep copy the plan config to avoid mutation
plan: dict[str, Any] = deepcopy(class_plan_config)
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

### 2. Each VM Is Resolved, Prepared, and Renamed

The fixture then resolves a per-test VM, applies source power-state rules, rewrites the VM name to the actual runtime name, and stores provider details separately:

```903:951:conftest.py
for vm in virtual_machines:
    # Get VM object first (without full vm_dict analysis)
    # Add enable_ctk flag for warm migrations
    clone_options = {**vm, "enable_ctk": warm_migration}
    provider_vm_api = source_provider.get_vm_by_name(
        query=vm["name"],
        vm_name_suffix=vm_name_suffix,
        clone_vm=True,
        session_uuid=fixture_store["session_uuid"],
        clone_options=clone_options,
    )

    # Power state control: "on" = start VM, "off" = stop VM, not set = leave unchanged
    source_vm_power = vm.get("source_vm_power")  # Optional - if not set, VM power state unchanged
    if source_vm_power == "on":
        source_provider.start_vm(provider_vm_api)
        # Wait for guest info to become available (VMware only)
        if source_provider.type == Provider.ProviderType.VSPHERE:
            source_provider.wait_for_vmware_guest_info(provider_vm_api, timeout=120)
    elif source_vm_power == "off":
        source_provider.stop_vm(provider_vm_api)

    # NOW call vm_dict() with VM in correct power state for guest info
    source_vm_details = source_provider.vm_dict(
        provider_vm_api=provider_vm_api,
        name=vm["name"],
        namespace=source_vms_namespace,
        clone=False,  # Already cloned above
        vm_name_suffix=vm_name_suffix,
        session_uuid=fixture_store["session_uuid"],
        clone_options=vm,
    )
    vm["name"] = source_vm_details["name"]

    # Wait for cloned VM to appear in Forklift inventory before proceeding
    # This is needed for external providers that Forklift needs to sync from
    # OVA is excluded because it doesn't clone VMs (uses pre-existing files)
    if source_provider.type != Provider.ProviderType.OVA:
        source_provider_inventory.wait_for_vm(name=vm["name"], timeout=300)

    provider_vm_api = source_vm_details["provider_vm_api"]

    vm["snapshots_before_migration"] = source_vm_details["snapshots_data"]
    # Store complete source VM data separately (keeps virtual_machines clean for Plan CR serialization)
    plan["source_vms_data"][vm["name"]] = source_vm_details

# Create Hooks if configured
create_hook_if_configured(plan, "pre_hook", "pre", fixture_store, ocp_admin_client, target_namespace)
create_hook_if_configured(plan, "post_hook", "post", fixture_store, ocp_admin_client, target_namespace)
```

### 3. The Test Adds VM IDs and Creates the MTV Plan

Right before creating the MTV `Plan`, the tests add VM IDs from Forklift inventory and then pass the prepared values into `create_plan_resource()`:

```184:202:tests/test_warm_migration_comprehensive.py
populate_vm_ids(plan=prepared_plan, inventory=source_provider_inventory)

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
)
```

## Runtime Fields Added During Preparation

These fields are derived at runtime. You do **not** write them yourself in `tests_params`.

| Field | Added by | Purpose |
| --- | --- | --- |
| `source_vms_data` | `prepared_plan` | Stores rich source VM details for later validation without polluting `virtual_machines`. |
| `_vm_target_namespace` | `prepared_plan` | Resolved namespace used by post-migration validation. |
| `_pre_hook_name` / `_pre_hook_namespace` | Hook creation | Created Hook CR reference for plan creation. |
| `_post_hook_name` / `_post_hook_namespace` | Hook creation | Created Hook CR reference for plan creation. |
| `virtual_machines[*].name` | `prepared_plan` | Rewritten to the actual runtime VM name after provider lookup or cloning. |
| `virtual_machines[*].snapshots_before_migration` | `prepared_plan` or snapshot test setup | Snapshot baseline used for post-migration checks. |
| `virtual_machines[*].id` | `populate_vm_ids()` | Forklift VM ID required by plan creation. |

A few raw config fields are also resolved by companion fixtures instead of `prepared_plan` itself:

- `multus_namespace` is consumed by the network setup fixture that creates NADs.
- `target_labels` is resolved by `target_vm_labels`.
- `target_node_selector` is resolved by `labeled_worker_node`.

> **Tip:** Keep `tests_params` declarative. Do not hand-write runtime fields such as `id`, `source_vms_data`, `_vm_target_namespace`, or `snapshots_before_migration`. Let the fixtures derive them.

## Practical Authoring Guidelines

- Start with one VM and one or two plan-level flags, then add more only when the scenario really needs them.
- Treat `virtual_machines` as source-side intent and `prepared_plan` as runtime state. They are not the same thing.
- Use `None` intentionally in `target_labels` or `target_node_selector` when you want a unique value per run.
- Expect VM names in `prepared_plan["virtual_machines"]` to differ from the raw `name` once preparation is complete.
- For copy-offload scenarios, think in two layers: test plan config in `tests_params`, and provider/storage config in `.providers.json`.
