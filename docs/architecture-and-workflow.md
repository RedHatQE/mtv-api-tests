# Architecture And Workflow

`mtv-api-tests` is built around complete migration lifecycles, not isolated unit tests. A typical test run resolves source and destination providers, waits for Forklift inventory to discover what it should migrate, creates `StorageMap` and `NetworkMap` objects, creates an MTV `Plan`, runs a `Migration`, validates the migrated VM or VMs, and then removes both cluster-side and provider-side leftovers.

> **Warning:** These are live integration tests. The repository’s built-in automation only does dry-run checks such as collection and setup planning. A real migration run needs a reachable OpenShift cluster with MTV installed, a valid source provider, credentials, storage, and networking.

> **Tip:** If you want a concrete reference while reading this page, start with `tests/test_mtv_cold_migration.py`, `tests/test_cold_migration_comprehensive.py`, and `tests/test_warm_migration_comprehensive.py`. Together they show the normal workflow and most optional features.

## Runtime Inputs

Two configuration layers drive everything:

- `tests/tests_config/config.py` is loaded automatically by `pytest.ini` and holds cluster-wide settings plus per-test plan dictionaries under `tests_params`.
- `.providers.json` selects the actual source provider profile. The repository includes `.providers.json.example` as the field reference for supported source types such as `vsphere`, `ovirt`, `openstack`, `openshift`, and `ova`, plus optional `copyoffload` settings.

A representative plan config looks like this:

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

That single config entry already tells you a lot about the architecture. The same workflow can change behavior through plan fields such as `warm_migration`, `preserve_static_ips`, `pvc_name_template`, `target_labels`, `target_affinity`, `target_node_selector`, and `vm_target_namespace`.

## The Standard Test Shape

Most migration tests follow the same five-step pattern: create storage map, create network map, create plan, execute migration, validate VMs.

```17:147:tests/test_mtv_cold_migration.py
@pytest.mark.tier0
@pytest.mark.incremental
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
@pytest.mark.usefixtures("cleanup_migrated_vms")
class TestSanityColdMtvMigration:
    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(...):
        self.__class__.storage_map = get_storage_migration_map(...)
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(...):
        self.__class__.network_map = get_network_migration_map(...)
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(...):
        populate_vm_ids(prepared_plan, source_provider_inventory)
        self.__class__.plan_resource = create_plan_resource(...)
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(...):
        execute_migration(...)

    def test_check_vms(...):
        check_vms(...)
```

This class-based layout is intentional. Earlier methods create resources that later methods depend on, and `@pytest.mark.incremental` prevents the test from pretending the later stages are meaningful after an earlier failure.

## 1. Session Bootstrap And Provider Setup

Before any migration-specific method runs, session fixtures do the shared setup work:

- `pytest_sessionstart()` validates required settings such as `storage_class` and `source_provider`.
- `target_namespace` creates a unique OpenShift namespace for the run.
- `forklift_pods_state` waits until the Forklift pods are healthy.
- `virtctl_binary` downloads and caches `virtctl`.
- `source_provider_data` resolves the selected source provider from `.providers.json`.

The source provider setup is centered around `utilities/utils.py:create_source_provider()`. It creates the source `Secret`, creates the source `Provider` custom resource, waits until that `Provider` is ready in Forklift, and only then opens the matching provider SDK wrapper.

```213:324:utilities/utils.py
secret_string_data = {
    "url": source_provider_data_copy["api_url"],
    "insecureSkipVerify": "true" if insecure else "false",
}
provider_args = {
    "username": source_provider_data_copy["username"],
    "password": source_provider_data_copy["password"],
    "fixture_store": fixture_store,
}

if ocp_provider(provider_data=source_provider_data_copy):
    source_provider = OCPProvider
    source_provider_data_copy["api_url"] = ocp_admin_client.configuration.host
    source_provider_data_copy["type"] = Provider.ProviderType.OPENSHIFT
    source_provider_secret = destination_ocp_secret

elif vmware_provider(provider_data=source_provider_data_copy):
    source_provider = VMWareProvider
    provider_args["host"] = source_provider_data_copy["fqdn"]
    secret_string_data["user"] = source_provider_data_copy["username"]
    secret_string_data["password"] = source_provider_data_copy["password"]
    if not insecure:
        _fetch_and_store_cacert(source_provider_data_copy, secret_string_data, tmp_dir, session_uuid)

# ... RHV, OpenStack, and OVA branches omitted ...

source_provider_secret = create_and_store_resource(
    fixture_store=fixture_store,
    resource=Secret,
    client=admin_client,
    namespace=namespace,
    string_data=secret_string_data,
    label=metadata_labels,
)

ocp_resource_provider = create_and_store_resource(
    fixture_store=fixture_store,
    resource=Provider,
    client=admin_client,
    namespace=namespace,
    secret_name=source_provider_secret.name,
    secret_namespace=namespace,
    url=source_provider_data_copy["api_url"],
    provider_type=source_provider_data_copy["type"],
    vddk_init_image=source_provider_data_copy.get("vddk_init_image"),
    annotations=provider_annotations or None,
)
ocp_resource_provider.wait_for_status(Provider.Status.READY, timeout=600, stop_status="ConnectionFailed")
```

A few important details come from this design:

- The tests always create real Forklift `Provider` resources first. The provider SDK wrappers are helpers, not the system of record.
- vSphere, RHV, OpenStack, OpenShift, and OVA all share the same outer workflow, even though their provider-specific secrets and connection logic differ.
- Remote OpenShift destination tests reuse the same basic pattern, but switch from `destination_provider` to `destination_ocp_provider`.

## 2. Forklift Inventory Discovery And Plan Preparation

Once the source `Provider` exists, the suite creates a `ForkliftInventory` adapter for that provider type in `conftest.py`. This is the layer that queries the `forklift-inventory` route and turns Forklift’s discovered objects into storage and network mappings.

The class-scoped `prepared_plan` fixture is where the user-facing plan config becomes an actual migration input. It copies the config, creates custom namespaces if requested, clones or resolves source VMs, adjusts their source power state, stores source-side metadata, and then waits until Forklift inventory can see the exact VM it will migrate.

```840:952:conftest.py
plan: dict[str, Any] = deepcopy(class_plan_config)
virtual_machines: list[dict[str, Any]] = plan["virtual_machines"]
warm_migration = plan.get("warm_migration", False)
plan["source_vms_data"] = {}

vm_target_namespace = plan.get("vm_target_namespace")
if vm_target_namespace:
    get_or_create_namespace(...)
    plan["_vm_target_namespace"] = vm_target_namespace
else:
    plan["_vm_target_namespace"] = target_namespace

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
        if source_provider.type == Provider.ProviderType.VSPHERE:
            source_provider.wait_for_vmware_guest_info(provider_vm_api, timeout=120)
    elif source_vm_power == "off":
        source_provider.stop_vm(provider_vm_api)

    source_vm_details = source_provider.vm_dict(...)
    vm["name"] = source_vm_details["name"]

    if source_provider.type != Provider.ProviderType.OVA:
        source_provider_inventory.wait_for_vm(name=vm["name"], timeout=300)

    plan["source_vms_data"][vm["name"]] = source_vm_details

create_hook_if_configured(plan, "pre_hook", "pre", fixture_store, ocp_admin_client, target_namespace)
create_hook_if_configured(plan, "post_hook", "post", fixture_store, ocp_admin_client, target_namespace)
```

This is one of the most important parts of the whole project. The test does not rush from “provider-side clone exists” to “create the Plan.” It waits until Forklift inventory has caught up.

> **Note:** RHV/oVirt is the main exception to the normal “inventory first” story for networks. Those tests clone from templates, so early network discovery comes from the RHV template API rather than Forklift inventory.

> **Note:** OpenStack inventory waits are stricter than a simple VM name lookup. The inventory adapter also waits until attached volumes and networks are queryable, because StorageMap and NetworkMap generation depend on that metadata.

## 3. StorageMap And NetworkMap

After `prepared_plan` is ready, the suite creates the two map resources that make the rest of the workflow possible.

For `StorageMap`, the standard path is:

- Ask Forklift inventory which source storages the selected VM or VMs use.
- Map each of those source storages to the configured destination `storage_class`.

For `NetworkMap`, the rule is deterministic and simple:

- The first source network maps to the destination pod network.
- Every additional source network maps to a class-scoped Multus network attachment definition.
- If the plan sets `multus_namespace`, those NADs can live outside the main test namespace.

That behavior comes from `utilities/utils.py:gen_network_map_list()` and the `multus_network_name` fixture in `conftest.py`.

A subtle but important detail happens right before Plan creation: `utilities/utils.py:populate_vm_ids()` injects Forklift inventory IDs into the VM list. The Plan is not built from names alone.

> **Tip:** If a VM has only one NIC, no extra NADs are created. The Multus path only appears for the second and later source networks.

## 4. Plan Creation

`utilities/mtv_migration.py:create_plan_resource()` is the assembly point where providers, maps, VM IDs, and optional plan features become an MTV `Plan` custom resource.

```200:259:utilities/mtv_migration.py
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

if copyoffload:
    plan_kwargs["pvc_name_template"] = "pvc"

plan = create_and_store_resource(**plan_kwargs)
plan.wait_for_condition(condition=Plan.Condition.READY, status=Plan.Condition.Status.TRUE, timeout=360)

if copyoffload:
    wait_for_plan_secret(ocp_admin_client, target_namespace, plan.name)
```

A few things to notice:

- The `Plan` resource itself lives in the test namespace, but the VMs can still target a separate `vm_target_namespace`.
- Hooks, labels, affinity, target power state, PVC naming, and static IP preservation are all Plan-time features in this suite.
- Copy-offload reuses the same overall path, but changes how the storage map is built and waits for a Forklift-created plan secret afterward.

## 5. Migration Execution

Migration execution is intentionally small in the test code: the heavy lifting is delegated to MTV.

`execute_migration()` creates a `Migration` CR that references the prepared `Plan`, then `wait_for_migration_complate()` polls the Plan status until it becomes `Succeeded` or `Failed`.

Cold migrations create the `Migration` immediately. Warm migrations do two extra things:

- They use `precopy_interval_forkliftcontroller` to patch the `ForkliftController` precopy interval.
- They pass `get_cutover_value()` when creating the `Migration`, which schedules cutover using `mins_before_cutover` from `tests/tests_config/config.py`.

> **Tip:** In this project, warm migration is not just `warm_migration=True` on the Plan. The test also sets a cutover timestamp on the `Migration` resource.

## 6. Post-Migration Validation

`utilities/post_migration.py:check_vms()` is the main validator. It re-reads the source VM and the destination VM through the provider abstraction layer, runs a wide set of checks, collects all failures per VM, and only then fails the test.

```1151:1316:utilities/post_migration.py
for vm in plan["virtual_machines"]:
    vm_name = vm["name"]
    source_vm = source_provider.vm_dict(
        name=vm_name,
        namespace=source_vms_namespace,
        source=True,
        source_provider_inventory=source_provider_inventory,
    )
    destination_vm = destination_provider.vm_dict(...)

    check_vms_power_state(...)
    check_cpu(...)
    check_memory(...)

    if source_provider.type != Provider.ProviderType.OPENSHIFT:
        check_network(...)

    check_storage(...)

    if plan.get("pvc_name_template"):
        check_pvc_names(...)

    if source_provider.type == Provider.ProviderType.VSPHERE:
        check_snapshots(...)
        check_serial_preservation(...)

    if vm_guest_agent:
        check_guest_agent(...)

    if vm_ssh_connections and destination_vm.get("power_state") == "on":
        check_ssh_connectivity(...)
        if ...:
            check_static_ip_preservation(...)

    if plan.get("target_node_selector") and labeled_worker_node:
        check_vm_node_placement(...)
    if plan.get("target_labels") and target_vm_labels:
        check_vm_labels(...)
    if plan.get("target_affinity"):
        check_vm_affinity(...)
```

Depending on the plan and provider, validation can include:

- Power state, CPU, and memory checks.
- Network verification against the created `NetworkMap`.
- Storage verification against the created `StorageMap`.
- PVC naming checks for `pvc_name_template`.
- VMware snapshot checks.
- VMware serial preservation checks.
- Guest agent verification.
- SSH connectivity to the migrated guest.
- Static IP preservation for Windows VMs migrated from vSphere.
- Target node placement, labels, and affinity.
- RHV-specific regression checks around unexpected source power-off behavior.

This is why `prepared_plan["source_vms_data"]` matters: it preserves source-side facts that are needed later for snapshot, PVC-name, and static-IP comparisons.

## 7. Advanced Paths

The normal workflow stays the same, but a few features add important branches.

Warm migration changes execution timing. The Plan is warm, the source-side clone can have Change Block Tracking enabled, the Forklift precopy interval is patched, and the `Migration` is created with a cutover timestamp.

Hooks are created during `prepared_plan` by `utilities/hooks.py:create_hook_if_configured()`. A plan can define `pre_hook` or `post_hook` with either a predefined success or failure playbook or a custom base64-encoded Ansible playbook. `tests/test_post_hook_retain_failed_vm.py` shows the intended behavior: a pre-hook failure can stop VM validation because the migration never really happened, while a post-hook failure can still leave migrated VMs behind and therefore still run `check_vms()`.

Copy-offload keeps the same outer sequence but changes the storage side. In that mode:

- The source provider is still vSphere.
- The storage secret comes from the plan’s `copyoffload` configuration and optional environment-variable overrides.
- `get_storage_migration_map()` uses datastore IDs and `offloadPlugin` entries instead of the standard inventory-derived storage list.
- The workflow can expand to secondary or non-XCOPY datastores for multi-datastore and fallback scenarios.
- If the provider requests SSH-based ESXi cloning, the setup fixtures install an SSH key before the migration and remove it afterward.

Remote OpenShift destination tests also reuse the same pattern. The difference is mostly in which destination provider fixture they use, not in the rest of the migration flow.

## 8. Teardown And Failure Handling

Cleanup is just as structured as setup.

Almost every OpenShift-side resource is created through `utilities/resources.py:create_and_store_resource()`, which deploys the resource and records it in `fixture_store["teardown"]`. Class-level cleanup removes migrated VMs early, and session-level cleanup handles everything else.

```107:162:utilities/pytest_utils.py
def session_teardown(session_store: dict[str, Any]) -> None:
    LOGGER.info("Running teardown to delete all created resources")
    ocp_client = get_cluster_client()

    if session_teardown_resources := session_store.get("teardown"):
        for migration_name in session_teardown_resources.get(Migration.kind, []):
            migration = Migration(name=migration_name["name"], namespace=migration_name["namespace"], client=ocp_client)
            cancel_migration(migration=migration)

        for plan_name in session_teardown_resources.get(Plan.kind, []):
            plan = Plan(name=plan_name["name"], namespace=plan_name["namespace"], client=ocp_client)
            archive_plan(plan=plan)

        leftovers = teardown_resources(
            session_store=session_store,
            ocp_client=ocp_client,
            target_namespace=session_store.get("target_namespace"),
        )
        if leftovers:
            raise SessionTeardownError(f"Failed to clean up the following resources: {leftovers}")

# inside teardown_resources(...)
migrations = session_teardown_resources.get(Migration.kind, [])
plans = session_teardown_resources.get(Plan.kind, [])
providers = session_teardown_resources.get(Provider.kind, [])
secrets = session_teardown_resources.get(Secret.kind, [])
networkmaps = session_teardown_resources.get(NetworkMap.kind, [])
storagemaps = session_teardown_resources.get(StorageMap.kind, [])
virtual_machines = session_teardown_resources.get(VirtualMachine.kind, [])
```

Session teardown does more than delete a few CRs:

- It cancels still-running migrations.
- It archives Plans before deletion.
- It deletes tracked `Provider`, `Secret`, `StorageMap`, `NetworkMap`, `Namespace`, `Migration`, and `VirtualMachine` resources.
- It waits for `DataVolume`, `PVC`, and `PV` cleanup.
- It reconnects to source providers such as vSphere, OpenStack, and RHV to delete cloned source-side VMs and snapshots that were created for the test.

If `--skip-teardown` is set, the class and session cleanup paths intentionally leave resources behind.

> **Note:** Failure handling is broader than cleanup. When data collection is enabled, the session writes created resources to `resources.json`, and failure paths can trigger `must-gather` collection so you can inspect what MTV and the cluster were doing at the time of failure.

## Automation And Dry Runs

The repository includes local automation, but it is deliberately conservative.

`pytest.ini` wires in `tests/tests_config/config.py`, enables strict markers, produces JUnit XML, and uses `loadscope` distribution. `tox.toml` does not try to run a real migration. Instead, it runs `pytest --setup-plan` and `pytest --collect-only`, which are useful for validating test discovery, parametrization, and fixture wiring without depending on a live MTV environment.

That split is a good way to think about the project as a whole:

- Configuration chooses the source provider and migration shape.
- Fixtures turn that configuration into discoverable Forklift and OpenShift resources.
- Tests create maps, Plans, and Migrations in a fixed order.
- Validation compares source-side and destination-side reality.
- Teardown removes everything the run created, both in the cluster and on the source side when needed.

If you keep that control loop in mind, the rest of the repository becomes much easier to navigate.
