# Cold Migrations

Cold migration tests in `mtv-api-tests` follow a consistent pattern: prepare the source VM data, create a `StorageMap`, create a `NetworkMap`, create a `Plan`, execute a
`Migration`, and then validate the migrated VM on the destination side. If you understand that flow, you understand the repository's standard cold migration pattern.

These are real integration tests, not unit tests. They create actual MTV and OpenShift resources, talk to a real source provider through Forklift inventory, and verify the
migrated VM after the move finishes. The MTV version under test is pinned in the `MTV-VERSION` file at the repository root (currently `2.12`).

## Required Inputs

A standard cold migration needs:

- A plan entry in `tests/tests_config/config.py`
- A source-provider entry in `.providers.json`
- Session config that includes `source_provider` and `storage_class`

The two values are enforced at session start. `pytest_sessionstart` exits before any fixture setup if either is missing:

```157:169:conftest.py
required_config = ("storage_class", "source_provider")

if not is_dry_run(session.config):
    BASIC_LOGGER.info(f"{separator(symbol_='-', val='SESSION START')}")

    missing_configs: list[str] = []

    for _req in required_config:
        if not py_config.get(_req):
            missing_configs.append(_req)

    if missing_configs:
        pytest.exit(reason=f"Some required config is missing {required_config=} - {missing_configs=}", returncode=1)
```

This is the sanctioned exception to the direct-access rule for controlled config: `.get()` is used only to gather *every* missing key so the operator sees the whole
list at once, and the run still fails before any fixture executes. It never substitutes a default for a value the suite then acts on.

The basic cold-migration test configuration is intentionally small:

```49:55:tests/tests_config/config.py
"test_sanity_cold_mtv_migration": {
    "virtual_machines": [
        {"name": "mtv-tests-rhel8", "guest_agent": True, "add_nic": True, "add_nic_start_connected": False},
    ],
    "warm_migration": False,
    "per_nic_network_map": True,
},
```

`add_nic` is a vSphere-only option. The `prepared_plan` fixture skips the whole class for any other provider type, and it fails fast when `add_nic_start_connected` is missing or
is not a `bool`:

```1148:1162:conftest.py
has_add_nic_config = any(vm.get("add_nic", False) for vm in virtual_machines)
if has_add_nic_config:
    if not isinstance(source_provider, VMWareProvider):
        pytest.skip(f"add_nic is vSphere-only; skipping for provider '{source_provider.type}'")
    for vm in virtual_machines:
        if vm.get("add_nic"):
            if "add_nic_start_connected" not in vm:
                raise ValueError(
                    f"VM '{vm['name']}': add_nic=True requires add_nic_start_connected to be set explicitly"
                )
            if not isinstance(vm["add_nic_start_connected"], bool):
                raise ValueError(
                    f"VM '{vm['name']}': add_nic_start_connected must be a bool, "
                    f"got {type(vm['add_nic_start_connected']).__name__!r}"
                )
```

Provider details come from `.providers.json`. The example file shows the fields the suite expects, including guest credentials used for post-migration checks such as SSH
validation:

```3:17:.providers.json.example
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
    "luks_passphrase": "LUKS DISK ENCRYPTION PASSPHRASE",  # pragma: allowlist secret
    "vddk_init_image": "<PATH TO VDDK INIT IMAGE>",
    "endpoint_type": "vcenter",
},
```

> **Note:** `.providers.json.example` contains inline comments for documentation. Remove those comments in your real `.providers.json`, because JSON does not support comments.

## The Standard Flow

`TestSanityColdMtvMigration` in `tests/cold/test_mtv_cold_migration.py` is the reference cold class. It has exactly five test methods:

1. `test_create_storagemap` creates the `StorageMap`.
2. `test_create_networkmap` creates the `NetworkMap`.
3. `test_create_plan` creates the `Plan`.
4. `test_migrate_vms` creates the `Migration` CR and waits for completion.
5. `test_check_vms` validates the migrated VM.

The class is marked `@pytest.mark.incremental`, so later stages only make sense after earlier ones succeed. It also uses `cleanup_migrated_vms`, which removes migrated VMs after
the class finishes unless `--skip-teardown` is passed:

```1629:1639:conftest.py
@pytest.fixture(scope="class")
def cleanup_migrated_vms(
    request: pytest.FixtureRequest,
    ocp_admin_client: DynamicClient,
    target_namespace: str,
    prepared_plan: dict[str, Any],
) -> Generator[None, None, None]:
    """Cleanup migrated VMs after test class completes.

    Teardown-only fixture that deletes VMs migrated during the test class.
    Honors --skip-teardown flag. Session teardown handles any leftovers.
```

`prepared_plan` is annotated `dict[str, Any]` on purpose: it is the mutated `class_plan_config` dict, and its shape depends on which optional plan flags a test
set — `warm_migration`, `copyoffload`, `xfs_check`, `copyoffload`, `target_affinity` and the rest appear only in the configs that use them. The keys read below are
the ones the fixture guarantees on every run, which is why they can be indexed directly even though the type is `Any`.

```1639:1659:conftest.py
yield

if request.config.getoption("skip_teardown"):
    LOGGER.info("Skipping VM cleanup due to --skip-teardown flag")
    return

# Use custom namespace if configured, otherwise fall back to target_namespace
vm_namespace = prepared_plan.get("_vm_target_namespace", target_namespace)

for vm in prepared_plan["virtual_machines"]:
    vm_name = resolve_destination_vm_name(vm)
    vm_obj = VirtualMachine(
        client=ocp_admin_client,
        name=vm_name,
        namespace=vm_namespace,
    )
    if vm_obj.exists:
        LOGGER.info(f"Cleaning up migrated VM: {vm_name} from namespace: {vm_namespace}")
        vm_obj.clean_up()
    else:
        LOGGER.info(f"VM {vm_name} already deleted from namespace: {vm_namespace}, skipping cleanup")
```

The same five-step shape is reused by the remote-cluster cold class `TestColdRemoteOcp` in the same file. The only meaningful difference there is the destination provider
fixture: it takes `destination_ocp_provider` instead of `destination_provider`, and it is gated on `remote_ocp_cluster`.

Other cold classes extend the pattern with extra verification steps. `AGENTS.md` documents each of them, and the ones that matter for cold coverage are:

- **6-step XFS pattern** in `tests/cold/test_cold_migration_xfs.py`: storagemap -> networkmap -> plan -> migrate -> `test_verify_xfs_version` -> check_vms
- **6-step LUKS pattern** in `tests/luks/test_luks_cold_migration.py`: storagemap -> networkmap -> plan -> migrate -> `test_verify_luks_encryption` -> check_vms
- **6-step shared-disk pattern (Linux)** in `tests/shared_disk/test_shared_disk_rhel_migration.py`: storagemap -> networkmap -> plan -> migrate -> `test_verify_shared_disk_data`
  -> check_vms
- **7-step shared-disk pattern (Windows)** in `tests/shared_disk/test_shared_disk_windows_migration.py`: `test_label_shared_disk` -> storagemap -> networkmap -> plan -> migrate
  -> `test_verify_shared_disk_data` -> check_vms
- **4-step plan-readiness pattern** in `tests/cold/test_ca_crt_migration.py`: `test_verify_ca_crt_secret` -> storagemap -> networkmap -> plan. No migration is executed.
- **3-step plan-readiness pattern** in `tests/cold/test_dual_nic_cold_migration.py`: storagemap -> networkmap -> plan. Plan readiness alone proves the per-NIC NetworkMap is
  accepted.

## 1. Prepare The Plan

Before any map is created, the class-scoped `prepared_plan` fixture turns a small config entry into something the rest of the test class can use. It copies the config, resolves
the PVC name template for the current provider, decides where migrated VMs should land, prepares or clones the source VM, applies an optional power-state change, waits for
Forklift inventory to see the VM, and stores full source VM details for later validation.

```1064:1090:conftest.py
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

Each VM is then cloned, powered, and recorded:

```1284:1317:conftest.py
# Power state control: "on" = start VM, "off" = stop VM, not set = leave unchanged
source_vm_power = vm.get("source_vm_power")  # Optional - if not set, VM power state unchanged
if source_vm_power == "on":
    source_provider.start_vm(provider_vm_api)
    if source_provider.type == Provider.ProviderType.VSPHERE:
        source_provider.wait_for_vmware_guest_info(
            provider_vm_api, timeout=class_plan_config.get("guest_agent_timeout", 120)
        )
    elif source_provider.type == Provider.ProviderType.HYPERV:
        source_provider.wait_for_guest_network_config(
            vm=provider_vm_api,
            timeout=class_plan_config.get("guest_agent_timeout", 120),
        )
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
cloned_vm_names.append(vm["name"])

provider_vm_api = source_vm_details["provider_vm_api"]

vm["snapshots_before_migration"] = source_vm_details["snapshots_data"]
# Store complete source VM data separately (keeps virtual_machines clean for Plan CR serialization)
plan["source_vms_data"][vm["name"]] = source_vm_details
```

A few practical details matter here:

- `source_vm_power` is optional. If you do not set it, the fixture leaves the source VM power state unchanged.
- `source_vms_data` is where the suite keeps rich source-side details for later checks such as static IP and PVC-name validation.
- `_vm_target_namespace` can be different from the namespace that holds the migration resources. That distinction matters in advanced cold-migration scenarios.
- `preserve_static_ips: True` requires `source_vm_power: "on"` on every VM. The fixture raises a `ValueError` otherwise, because guest tools must be running to collect static IP
  and NIC data.

> **Tip:** If you only need baseline cold-migration coverage, start with the minimal config shown above and let `prepared_plan` do the rest.

## 2. Create The StorageMap

`test_create_storagemap()` takes the VM names from `prepared_plan` and passes them to `get_storage_migration_map()`. The helper does not hardcode source datastores or storage
domains. Instead, it asks Forklift inventory which storages those VMs actually use, then maps each one to the selected OpenShift `storage_class`.

```665:684:utilities/mtv_migration.py
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
return storage_map
```

The destination storage class comes from the `storage_class` parameter, falling back to the session config value. The same helper also has a copy-offload branch that adds
`offloadPlugin` entries instead of inventory-discovered datastores; that branch is covered on the [Copy-Offload Migrations](copy-offload-migrations.html) page.

This is why the cold tests stay fairly small at the test-method level. Storage discovery is delegated to the provider-specific inventory code in `libs/forklift_inventory.py`, so
the test only has to name the VM and the target storage class.

## 3. Create The NetworkMap

Network mapping follows the same inventory-driven idea. The suite asks Forklift inventory which source networks the chosen VM uses, then maps those networks to either the pod
network or class-scoped Multus networks.

The key rule is simple: the first network goes to the pod network, and every additional network is mapped to a Multus `NetworkAttachmentDefinition`.

```342:367:utilities/utils.py
for index, network in enumerate(networks):
    if pod_only or index == 0:
        # First network or pod_only mode → pod network
        _destination = _destination_pod
    else:
        # Extract base name and namespace from multus_network_name (only when needed)
        multus_network_name_str = multus_network_name["name"]
        multus_namespace = multus_network_name["namespace"]

        # Generate unique NAD name for each additional network
        # Use consistent naming: {base_name}-1, {base_name}-2, etc.
        # Where base_name includes unique test identifier (e.g., cnv-bridge-abc12345)
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
return network_map_list
```

That behavior is user-friendly in practice:

- Single-NIC VMs usually need no special network setup beyond the default pod network.
- Multi-NIC VMs automatically get additional Multus attachments.
- `per_nic_network_map: True` disables deduplication, so one NetworkMap entry is created per VM NIC. That is what allows two NICs attached to the same source network to map to
  different destinations without a `VMDuplicateNADMappings` plan error.
- If your config sets `multus_namespace`, the `multus_network_name` fixture creates those NADs in that namespace instead of the default migration namespace.

## 4. Create The Plan And Execute The Migration

Before creating the `Plan`, the cold tests call `populate_vm_ids()` so each VM in `virtual_machines` includes the Forklift inventory ID MTV expects. Then `create_plan_resource()`
builds a `Plan` CR that ties together the source provider, destination provider, storage map, network map, VM list, and any optional plan features.

The same helper also shows an important namespace detail: by default, migrated VMs land in the same `target_namespace`, but `vm_target_namespace` can override that when needed.

```281:296:utilities/mtv_migration.py
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
    "virtual_machines_list": vms_for_plan,
    "target_namespace": vm_target_namespace or target_namespace,
    "warm_migration": warm_migration,
```

`create_plan_resource()` then waits for the Plan to reach `Ready` with a hard-coded 360-second timeout, independent of `plan_wait_timeout`:

```340:346:utilities/mtv_migration.py
plan = create_and_store_resource(**plan_kwargs)

try:
    plan.wait_for_condition(condition=Plan.Condition.READY, status=Plan.Condition.Status.TRUE, timeout=360)
except TimeoutExpiredError:
    LOGGER.error(f"Plan {plan.name} failed to reach status {Plan.Condition.Status.TRUE}\n\t{plan.instance}")
```

`plan_wait_timeout` applies later, to the migration wait:

```382:392:utilities/mtv_migration.py
create_and_store_resource(
    client=ocp_admin_client,
    fixture_store=fixture_store,
    resource=Migration,
    namespace=target_namespace,
    plan_name=plan.name,
    plan_namespace=plan.namespace,
    cut_over=cut_over,
)

wait_for_migration_complate(plan=plan, on_status_poll=on_status_poll)
```

For standard cold migrations, the important switch is `warm_migration=False`. That means:

- There is no warm-only precopy fixture.
- `cut_over` is `None`, so the Migration CR has no scheduled cutover time.
- Execution goes directly from a ready `Plan` to a real `Migration` CR.

The config sets `plan_wait_timeout` to `3600` seconds by default in `tests/tests_config/config.py`, and `wait_for_migration_complate()` polls the Plan status until it reaches
`Succeeded` or `Failed`, raising `MigrationPlanExecError` on failure or timeout.

## 5. Validate The Migrated VM

The post-migration phase is much more than "the VM exists." The `check_vms()` helper fetches both the source VM and the destination VM, runs a series of validations, accumulates
any mismatches per VM, and fails at the end if anything is wrong.

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
- Nested-virtualization CPU features, when `enable_nested_virtualization` is `False`

Some checks are provider-specific:

- `check_ssl_configuration()` runs for vSphere, RHV, and OpenStack sources.
- Snapshot comparison, serial preservation, boot configuration, and disconnected-NIC state are used for VMware-backed migrations.
- False power-off validation is used for RHV-backed migrations.
- Static IP preservation runs for vSphere and Hyper-V sources. Inside the check, Windows guests are inspected with `ipconfig /all` and Linux guests with `nmcli device show`.

The default invocation passes `vm_ssh_connections` but not `plan_resource`, so the plan-driven Deep Inspection verification block is skipped unless a test opts in.

This validation step is where the cold migration pattern becomes genuinely useful. A migration can "finish" and still be wrong. The repository's cold tests treat success as "the
VM moved and still matches expectations," not just "the Migration CR reached a terminal state."

> **Note:** `cleanup_migrated_vms` removes migrated VMs after the class finishes. If you run with `--skip-teardown`, those VMs are intentionally left behind for debugging.

## Advanced Cold Migration Features

The comprehensive cold migration test shows how the same pattern scales up when you want to validate plan features, not just a successful move:

```705:742:tests/tests_config/config.py
"test_cold_migration_comprehensive": {
    "virtual_machines": [
        {
            "name": "mtv-win2019-3disks",
            "source_vm_power": "on",  # VM must be on for guest tools to report static IP info
            "guest_agent": True,
        },
    ],
    "warm_migration": False,
    "target_power_state": "on",
    "preserve_static_ips": True,
    "enable_nested_virtualization": False,
    "pvc_name_template": '{{ .VmName | trunc 32 | trimSuffix "-" }}-{{ .VmName | trunc -4 }}-disk-{{.DiskIndex}}',
    "pvc_name_template_use_generate_name": False,
    "target_node_selector": {
        "mtv-comprehensive-node": None,  # None = auto-generate with session_uuid
    },
    "target_labels": {
        "mtv-comprehensive-label": None,  # None = auto-generate with session_uuid
        "test-type": "comprehensive",  # Static value
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
    "vm_target_namespace": f"mtv-vms-cold-comprehensive-{uuid.uuid4().hex[:4]}",
    "multus_namespace": "default",  # Cross-namespace NAD access
    "guest_agent_timeout": 600,
},
```

Each of those fields drives a real plan feature or a real validation path:

- `source_vm_power: "on"` powers the source VM on before cloning finishes so VMware guest tools can report static IP and NIC data. It is mandatory when `preserve_static_ips` is
  set.
- `target_power_state: "on"` proves that the destination VM comes up powered on.
- `preserve_static_ips: True` enables static-IP verification, and additionally NIC-name preservation for vSphere sources.
- `pvc_name_template` and `pvc_name_template_use_generate_name` turn on PVC-name validation after migration. A template can also be a provider-keyed mapping, resolved by
  `resolve_pvc_name_template()`.
- `target_node_selector` causes a worker node to be labeled for the test. The `labeled_worker_node` fixture picks the worker with the most available memory, appends the
  `session_uuid` to the label value when the config value is `None`, and post-migration validation checks that the migrated VM lands there.
- `target_labels` adds expected labels to the migrated VM. When a value is `None`, the `target_vm_labels` fixture replaces it with the current `session_uuid` so labels stay
  unique across runs.
- `target_affinity` lets the test verify full affinity configuration on the destination VM.
- `enable_nested_virtualization: False` makes the suite assert that CPU features such as `vmx` and `svm` are disabled on the migrated VM.
- `vm_target_namespace` separates the VM's destination namespace from the namespace that holds the MTV resources.
- `multus_namespace` lets the test create its NADs outside the default migration namespace.
- `guest_agent_timeout` overrides the default 120-second wait for VMware guest info during preparation, and is passed to the destination VM lookup in `check_vms()`.

> **Tip:** Use `test_sanity_cold_mtv_migration` when you want baseline cold-migration coverage. Use `test_cold_migration_comprehensive` when you want to validate plan behavior
> such as target power state, PVC naming, labels, affinity, or custom VM namespaces.

## How Automation Treats Cold Tests

The repository's automation is careful about the difference between "this suite is structurally valid" and "a real cold migration succeeded."

Pytest is wired through `pytest.ini` to load `tests/tests_config/config.py` automatically and to use `--dist=loadscope`, which fits the class-based, incremental cold-migration
pattern well.

The included `tox` environment does not perform a live migration run. Instead, it uses dry-run style checks:

```4:18:tox.toml
[env.pytest-check]
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

> **Warning:** A successful `tox` run or container dry-run only proves that the suite can be collected and its fixtures can be planned. It does not prove that a cold migration
> will succeed against your real OpenShift cluster, source provider, network setup, or storage class.

That split is important for users. The repository can validate test structure in automation, but real cold-migration confidence still comes from running the suite in a live
environment with real provider and cluster access.
