# Cold Migrations

Cold migration tests in `mtv-api-tests` follow a consistent pattern: prepare the source VM data, create a `StorageMap`, create a `NetworkMap`, create a `Plan`, execute a `Migration`, and then validate the migrated VM on the destination side. If you understand that flow, you understand the repository’s standard cold migration pattern.

These are real integration tests, not unit tests. They create actual MTV and OpenShift resources, talk to a real source provider through Forklift inventory, and verify the migrated VM after the move finishes.

## Required Inputs

A standard cold migration needs:

- A plan entry in `tests/tests_config/config.py`
- A source-provider entry in `.providers.json`
- Session config that includes `source_provider` and `storage_class`

The basic cold-migration test configuration is intentionally small:

```46:51:tests/tests_config/config.py
"test_sanity_cold_mtv_migration": {
    "virtual_machines": [
        {"name": "mtv-tests-rhel8", "guest_agent": True},
    ],
    "warm_migration": False,
},
```

Provider details come from `.providers.json`. The example file shows the fields the suite expects, including guest credentials used for post-migration checks such as SSH validation:

```2:14:.providers.json.example
"vsphere": {
  "type": "vsphere",
  "version": "<SERVER VERSION>",
  "fqdn": "SERVER FQDN/IP",
  "api_url": "<SERVER FQDN/IP>/sdk",
  "username": "USERNAME",
  "password": "PASSWORD",  # pragma: allowlist secret
  "guest_vm_linux_user": "LINUX VMS USERNAME",
  "guest_vm_linux_password": "LINUX VMS PASSWORD",  # pragma: allowlist secret
  "guest_vm_win_user": "WINDOWS VMS USERNAME",
  "guest_vm_win_password": "WINDOWS VMS PASSWORD",  # pragma: allowlist secret
  "vddk_init_image": "<PATH TO VDDK INIT IMAGE>"
},
```

> **Note:** `.providers.json.example` contains inline comments for documentation. Remove those comments in your real `.providers.json`, because JSON does not support comments.

At runtime, `conftest.py` also enforces `source_provider` and `storage_class` before the session starts, so a real cold-migration run needs both values in place.

## The Standard Flow

In `tests/test_mtv_cold_migration.py`, the class-based cold test follows the same six stages every time:

1. `prepared_plan` builds the class-scoped migration input.
2. `test_create_storagemap` creates the `StorageMap`.
3. `test_create_networkmap` creates the `NetworkMap`.
4. `test_create_plan` creates the `Plan`.
5. `test_migrate_vms` creates the `Migration` CR and waits for completion.
6. `test_check_vms` validates the migrated VM.

The class is marked `@pytest.mark.incremental`, so later stages only make sense after earlier ones succeed. It also uses `cleanup_migrated_vms`, which removes migrated VMs after the class finishes unless teardown is explicitly skipped.

The same shape is reused for the remote-cluster cold migration class as well. The only meaningful difference there is the destination provider fixture.

## 1. Prepare The Plan

Before any map is created, the `prepared_plan` fixture turns a small config entry into something the rest of the test class can use. It copies the config, decides where migrated VMs should land, prepares or clones the source VM, applies an optional power-state change, waits for Forklift inventory to see the VM, and stores full source VM details for later validation.

```840:947:conftest.py
plan: dict[str, Any] = deepcopy(class_plan_config)
virtual_machines: list[dict[str, Any]] = plan["virtual_machines"]
warm_migration = plan.get("warm_migration", False)

plan["source_vms_data"] = {}

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

# ... each VM is prepared before the Plan is created ...

for vm in virtual_machines:
    clone_options = {**vm, "enable_ctk": warm_migration}
    provider_vm_api = source_provider.get_vm_by_name(
        query=vm["name"],
        vm_name_suffix=vm_name_suffix,
        clone_vm=True,
        session_uuid=fixture_store["session_uuid"],
        clone_options=clone_options,
    )

    source_vm_power = vm.get("source_vm_power")
    if source_vm_power == "on":
        source_provider.start_vm(provider_vm_api)
    elif source_vm_power == "off":
        source_provider.stop_vm(provider_vm_api)

    source_vm_details = source_provider.vm_dict(
        provider_vm_api=provider_vm_api,
        name=vm["name"],
        namespace=source_vms_namespace,
        clone=False,
        vm_name_suffix=vm_name_suffix,
        session_uuid=fixture_store["session_uuid"],
        clone_options=vm,
    )
    vm["name"] = source_vm_details["name"]
    source_provider_inventory.wait_for_vm(name=vm["name"], timeout=300)
    plan["source_vms_data"][vm["name"]] = source_vm_details
```

A few practical details matter here:

- `source_vm_power` is optional. If you do not set it, the fixture leaves the source VM power state unchanged.
- `source_vms_data` is where the suite keeps rich source-side details for later checks such as static IP and PVC-name validation.
- `_vm_target_namespace` can be different from the namespace that holds the migration resources. That distinction matters in advanced cold-migration scenarios.

> **Tip:** If you only need baseline cold-migration coverage, start with the minimal config shown above and let `prepared_plan` do the rest.

## 2. Create The StorageMap

`test_create_storagemap()` takes the VM names from `prepared_plan` and passes them to `get_storage_migration_map()`. The helper does not hardcode source datastores or storage domains. Instead, it asks Forklift inventory which storages those VMs actually use, then maps each one to the selected OpenShift `storage_class`.

```429:505:utilities/mtv_migration.py
target_storage_class: str = storage_class or py_config["storage_class"]
storage_map_list: list[dict[str, Any]] = []

# ... copy-offload branch omitted ...

LOGGER.info(f"Creating standard storage map for VMs: {vms}")
storage_migration_map = source_provider_inventory.vms_storages_mappings(vms=vms)
for storage in storage_migration_map:
    storage_map_list.append({
        "destination": {"storageClass": target_storage_class},
        "source": storage,
    })

storage_map = create_and_store_resource(
    fixture_store=fixture_store,
    resource=StorageMap,
    client=ocp_admin_client,
    namespace=target_namespace,
    mapping=storage_map_list,
    source_provider_name=source_provider.ocp_resource.name,
    source_provider_namespace=source_provider.ocp_resource.namespace,
    destination_provider_name=destination_provider.ocp_resource.name,
    destination_provider_namespace=destination_provider.ocp_resource.namespace,
)
```

This is why the cold tests stay fairly small at the test-method level. Storage discovery is delegated to the provider-specific inventory code in `libs/forklift_inventory.py`, so the test only has to name the VM and the target storage class.

## 3. Create The NetworkMap

Network mapping follows the same inventory-driven idea. The suite asks Forklift inventory which source networks the chosen VM uses, then maps those networks to either the pod network or class-scoped Multus networks.

The key rule is simple: the first network goes to the pod network, and every additional network is mapped to a Multus `NetworkAttachmentDefinition`.

```154:190:utilities/utils.py
for index, network in enumerate(source_provider_inventory.vms_networks_mappings(vms=vms)):
    if pod_only or index == 0:
        _destination = {"type": "pod"}
    else:
        multus_network_name_str = multus_network_name["name"]
        multus_namespace = multus_network_name["namespace"]

        nad_name = f"{multus_network_name_str}-{multus_counter}"

        _destination = {
            "name": nad_name,
            "namespace": multus_namespace,
            "type": "multus",
        }
        multus_counter += 1

    network_map_list.append({
        "destination": _destination,
        "source": network,
    })
```

That behavior is user-friendly in practice:

- Single-NIC VMs usually need no special network setup beyond the default pod network.
- Multi-NIC VMs automatically get additional Multus attachments.
- If your config sets `multus_namespace`, the suite creates those NADs in that namespace instead of the default migration namespace.

## 4. Create The Plan And Execute The Migration

Before creating the `Plan`, the cold tests call `populate_vm_ids()` so each VM in `virtual_machines` includes the Forklift inventory ID MTV expects. Then `create_plan_resource()` builds a `Plan` CR that ties together the source provider, destination provider, storage map, network map, VM list, and any optional plan features.

The same helper also shows an important namespace detail: by default, migrated VMs land in the same `target_namespace`, but `vm_target_namespace` can override that when needed.

```200:295:utilities/mtv_migration.py
plan_kwargs: dict[str, Any] = {
    "client": ocp_admin_client,
    "fixture_store": fixture_store,
    "resource": Plan,
    "namespace": target_namespace,
    "source_provider_name": source_provider.ocp_resource.name,
    "source_provider_namespace": source_provider.ocp_resource.namespace,
    "destination_provider_name": destination_provider.ocp_resource.name,
    "destination_provider_namespace": destination_provider.ocp_resource.namespace,
    "storage_map_name": storage_map.name,
    "storage_map_namespace": storage_map.namespace,
    "network_map_name": network_map.name,
    "network_map_namespace": network_map.namespace,
    "virtual_machines_list": virtual_machines_list,
    "target_namespace": vm_target_namespace or target_namespace,
    "warm_migration": warm_migration,
    "pre_hook_name": pre_hook_name,
    "pre_hook_namespace": pre_hook_namespace,
    "after_hook_name": after_hook_name,
    "after_hook_namespace": after_hook_namespace,
    "preserve_static_ips": preserve_static_ips,
    "pvc_name_template": pvc_name_template,
    "pvc_name_template_use_generate_name": pvc_name_template_use_generate_name,
    "target_power_state": target_power_state,
}

if target_node_selector:
    plan_kwargs["target_node_selector"] = target_node_selector

if target_labels:
    plan_kwargs["target_labels"] = target_labels

if target_affinity:
    plan_kwargs["target_affinity"] = target_affinity

plan = create_and_store_resource(**plan_kwargs)
plan.wait_for_condition(condition=Plan.Condition.READY, status=Plan.Condition.Status.TRUE, timeout=360)

# ... later, execution creates the Migration CR ...

create_and_store_resource(
    client=ocp_admin_client,
    fixture_store=fixture_store,
    resource=Migration,
    namespace=target_namespace,
    plan_name=plan.name,
    plan_namespace=plan.namespace,
    cut_over=cut_over,
)

wait_for_migration_complate(plan=plan)
```

For standard cold migrations, the important switch is `warm_migration=False`. That means:

- There is no warm-only precopy fixture.
- There is no scheduled cutover time in the normal cold flow.
- Execution goes directly from a ready `Plan` to a real `Migration` CR.

By default, the config sets `plan_wait_timeout` to `3600` seconds in `tests/tests_config/config.py`, so long-running migrations have a wider execution window than the helper’s fallback default.

## 5. Validate The Migrated VM

The post-migration phase is much more than “the VM exists.” The `check_vms()` helper fetches both the source VM and the destination VM, runs a series of validations, accumulates any mismatches per VM, and fails at the end if anything is wrong.

In the standard cold flow, it checks:

- Power state
- CPU
- Memory
- Network mapping
- Storage class and disk mapping

When the plan config asks for more, it can also check:

- PVC names
- Guest-agent availability
- SSH connectivity to the migrated VM
- Static IP preservation
- Target node placement
- Target labels
- Affinity rules

Some checks are provider-specific:

- Snapshot comparison and serial preservation are used for VMware-backed migrations.
- False power-off validation is used for RHV-backed migrations.
- Static IP preservation is currently implemented for Windows VMs migrated from vSphere.

This validation step is where the cold migration pattern becomes genuinely useful. A migration can “finish” and still be wrong. The repository’s cold tests treat success as “the VM moved and still matches expectations,” not just “the Migration CR reached a terminal state.”

> **Note:** `cleanup_migrated_vms` removes migrated VMs after the class finishes. If you run with `--skip-teardown`, those VMs are intentionally left behind for debugging.

## Advanced Cold Migration Features

The comprehensive cold migration test shows how the same pattern scales up when you want to validate plan features, not just a successful move:

```467:502:tests/tests_config/config.py
"test_cold_migration_comprehensive": {
    "virtual_machines": [
        {
            "name": "mtv-win2019-3disks",
            "source_vm_power": "off",
            "guest_agent": True,
        },
    ],
    "warm_migration": False,
    "target_power_state": "on",
    "preserve_static_ips": True,
    "pvc_name_template": "{{.VmName}}-disk-{{.DiskIndex}}",
    "pvc_name_template_use_generate_name": False,
    "target_node_selector": {
        "mtv-comprehensive-node": None,
    },
    "target_labels": {
        "mtv-comprehensive-label": None,
        "test-type": "comprehensive",
    },
    "target_affinity": {
        "podAffinity": {
            "preferredDuringSchedulingIgnoredDuringExecution": [
                {
                    "podAffinityTerm": {
                        "labelSelector": {"matchLabels": {"app": "test"}},
                        "topologyKey": "kubernetes.io/hostname",
                    },
                    "weight": 50,
                }
            ]
        }
    },
    "vm_target_namespace": "mtv-comprehensive-vms",
    "multus_namespace": "default",
},
```

Each of those fields drives a real plan feature or a real validation path:

- `source_vm_power: "off"` tells the preparation fixture to power the source VM off before migration.
- `target_power_state: "on"` proves that the destination VM can come up powered on even if the source was prepared powered off.
- `preserve_static_ips: True` enables static-IP verification. In the current codebase, that check is meant for Windows VMs coming from vSphere.
- `pvc_name_template` and `pvc_name_template_use_generate_name` turn on PVC-name validation after migration. The helper also supports more advanced template behavior, including `generateName` handling.
- `target_node_selector` causes a worker node to be labeled for the test, and post-migration validation checks that the migrated VM lands there.
- `target_labels` adds expected labels to the migrated VM. When a value is `None`, the fixture replaces it with the current `session_uuid` so labels stay unique across runs.
- `target_affinity` lets the test verify full affinity configuration on the destination VM.
- `vm_target_namespace` separates the VM’s destination namespace from the namespace that holds the MTV resources.
- `multus_namespace` lets the test create its NADs outside the default migration namespace.

> **Tip:** Use `test_sanity_cold_mtv_migration` when you want baseline cold-migration coverage. Use `test_cold_migration_comprehensive` when you want to validate plan behavior such as target power state, PVC naming, labels, affinity, or custom VM namespaces.

## How Automation Treats Cold Tests

The repository’s automation is careful about the difference between “this suite is structurally valid” and “a real cold migration succeeded.”

Pytest is wired through `pytest.ini` to load `tests/tests_config/config.py` automatically and to use `--dist=loadscope`, which fits the class-based, incremental cold-migration pattern well.

The included `tox` environment does not perform a live migration run. Instead, it uses dry-run style checks:

```4:18:tox.toml
commands = [
  [
    "uv",
    "run",
    "pytest",
    "--setup-plan",
  ],
  [
    "uv",
    "run",
    "pytest",
    "--collect-only",
  ],
]
```

The container image follows the same philosophy: its default command is `uv run pytest --collect-only`.

> **Warning:** A successful `tox` run or container dry-run only proves that the suite can be collected and its fixtures can be planned. It does not prove that a cold migration will succeed against your real OpenShift cluster, source provider, network setup, or storage class.

That split is important for users. The repository can validate test structure in automation, but real cold-migration confidence still comes from running the suite in a live environment with real provider and cluster access.
