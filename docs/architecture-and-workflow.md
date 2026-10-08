# Architecture And Workflow

`mtv-api-tests` is built around complete migration lifecycles, not isolated unit tests. A typical test run resolves source and
destination providers, waits for Forklift inventory to discover what it should migrate, creates `StorageMap` and `NetworkMap`
objects, creates an MTV `Plan`, runs a `Migration`, validates the migrated VM or VMs, and then removes both cluster-side and
provider-side leftovers.

> **Warning:** These are live integration tests. The repository's built-in automation only does dry-run checks such as collection
> and setup planning. A real migration run needs a reachable OpenShift cluster with MTV installed, a valid source provider,
> credentials, storage, and networking.
>
> **Tip:** If you want a concrete reference while reading this page, start with `tests/cold/test_mtv_cold_migration.py`,
> `tests/cold/test_cold_migration_comprehensive.py`, and `tests/warm/test_warm_migration_comprehensive.py`. Together they show
> the normal workflow and most optional features.

## Runtime Inputs

Two configuration layers drive everything:

- `tests/tests_config/config.py` is loaded automatically by `pytest.ini` and holds cluster-wide defaults (`insecure_verify_skip`,
  `source_provider_insecure_skip_verify`, `number_of_vms`, `check_vms_signals`, `target_namespace_prefix`, `mtv_namespace`,
  `vm_name_search_pattern`, `remote_ocp_cluster`, `snapshots_interval`, `mins_before_cutover`, `plan_wait_timeout`) plus per-test
  plan dictionaries under `tests_params`. The two values `pytest_sessionstart` requires, `storage_class` and `source_provider`,
  are not in the file; pass them with `--tc storage_class:<name> --tc source_provider:<key>`. Not every key in the file is read
  by the suite today: `number_of_vms`, `check_vms_signals`, and `vm_name_search_pattern` currently have no reader in
  `conftest.py` or `utilities/`.
- `.providers.json` selects the actual source provider profile. The repository includes `.providers.json.example` as the field
  reference, validated against `providers_schema.json`, for source types `vsphere`, `ovirt`, `openstack`, `openshift`, `ova`,
  and `hyperv`, plus the vSphere-specific `copyoffload`, `endpoint_type`, and `clone_provider` settings.

A representative plan config looks like this:

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

That single config entry already tells you a lot about the architecture. The same workflow can change behavior through plan fields
such as `warm_migration`, `preserve_static_ips`, `pvc_name_template`, `target_labels`, `target_affinity`, `target_node_selector`,
`vm_target_namespace`, `enable_nested_virtualization`, and `guest_agent_timeout`. `AGENTS.md` carries the complete plan-option
table, which also includes `copyoffload`, `xfs_compatibility`, `migrate_shared_disks`, `inventory_timeout`, `clone_to_same_host`,
`pin_to_non_dedicated_host`, `disable_drs_for_vms`, `per_nic_network_map`, `skip_clone`, and `rdm_as_lun`.

## The Standard Test Shape

Most migration tests follow the same five-step pattern: create storage map, create network map, create plan, execute migration,
validate VMs. Condensed from `tests/cold/test_mtv_cold_migration.py:19-147`:

```python
@pytest.mark.tier0
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_sanity_cold_mtv_migration"])],
    indirect=True,
    ids=["rhel8"],
)
@pytest.mark.usefixtures("cleanup_migrated_vms")
class TestSanityColdMtvMigration:
    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(self, prepared_plan: dict[str, Any], fixture_store: dict[str, Any], ...) -> None:
        """Create StorageMap resource for migration."""
        self.__class__.storage_map = get_storage_migration_map(...)
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(self, prepared_plan: dict[str, Any], multus_network_name: str, ...) -> None:
        """Create NetworkMap resource for migration."""
        self.__class__.network_map = get_network_migration_map(...)
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan: dict[str, Any],
        source_provider_inventory: ForkliftInventory,
        ...,
    ) -> None:
        """Create MTV Plan CR resource."""
        populate_vm_ids(prepared_plan, source_provider_inventory)
        self.__class__.plan_resource = create_plan_resource(...)
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(self, ocp_admin_client: DynamicClient, target_namespace: str, ...) -> None:
        """Execute migration."""
        execute_migration(...)

    def test_check_vms(self, prepared_plan: dict[str, Any], vm_ssh_connections: SSHConnectionManager) -> None:
        """Validate migrated VMs."""
        check_vms(...)
```

Every parameter above is a pytest fixture, so the annotations document what each step consumes. The suite's own test methods are currently written
without parameter annotations - that is existing practice, not the rule; new framework code and helpers are annotated, and annotating test methods costs nothing.

Note the shape of the two middle steps. `test_create_plan` calls `populate_vm_ids(prepared_plan, source_provider_inventory)`
before `create_plan_resource()`, and `test_migrate_vms` requests only the fixtures it needs, because the resource it waits on is
already stored on the class.

This class-based layout is intentional. Earlier methods create resources that later methods depend on, and
`@pytest.mark.incremental` prevents the test from pretending the later stages are meaningful after an earlier failure:
`pytest_runtest_makereport` records the failure and `pytest_runtest_setup` turns it into an `xfail` for the rest of the class.

### Extended Step Patterns

Feature suites extend or replace the base steps. `AGENTS.md` under "Key Patterns" is the authoritative list and names every test
method; the variants actually present in `tests/` are:

- **4-step plan-readiness**: a leading `test_verify_<feature>` step replaces migrate/check, because the feature under test is
  exercised during provider or Plan creation and Plan readiness proves it works. No migration is executed. Used by
  `tests/cold/test_ca_crt_migration.py` and `tests/cold/test_insecure_skip_verify_migration.py`.
- **6-step plan-archive PVC cleanup**: `test_migrate_vms` expects `MigrationPlanExecError`, then `test_archive_and_delete_plan`
  and `test_verify_pvc_cleanup`. There is no `test_check_vms`. Lives in `tests/plan_lifecycle/`.
- **6-step LUKS**: `test_verify_luks_encryption` before `test_check_vms`. Lives in `tests/luks/`.
- **6-step XFS**: `test_verify_xfs_version` before `test_check_vms`. Lives in `tests/cold/test_cold_migration_xfs.py` and
  `tests/warm/test_warm_migration_xfs.py`.
- **6-step shared disk (Linux)**: `test_verify_shared_disk_data` before `test_check_vms`.
- **7-step shared disk (Windows)**: `test_label_shared_disk` first, then `test_verify_shared_disk_data` before `test_check_vms`.
- **6-step copy-offload**: `test_check_xcopy_used` before `test_check_vms`. Snapshot subclasses add
  `test_create_snapshots_and_data_marker` first and `test_check_data_integrity` last.
- **7/8-step copy-offload throttling**: `test_verify_populator_throttling` and `test_verify_vm_inflight_throttling` after
  `test_migrate_vms`.
- **7-step copy-offload dedicated host**: `test_verify_dedicated_migration_host` after `test_migrate_vms`.
- **Deep Inspection, standalone**: `test_create_conversion`, `test_wait_for_completion`, `test_verify_di_results`. The cancel path
  adds `test_cancel_conversion`, `test_verify_cancel_cleanup`, `test_verify_snapshot_cleanup`, and a rerun.
- **Deep Inspection, plan-driven warm**: `test_migrate_vms_fails` then `test_verify_di_concerns`.
- **Upgrade**: `test_upgrade_mtv`, `test_verify_post_upgrade`, then migrate and check. Maps and Plan come from
  `tests/upgrade/conftest.py` fixtures rather than test methods.

## 1. Session Bootstrap And Provider Setup

Before any migration-specific method runs, session fixtures and hooks do the shared setup work:

- `pytest_sessionstart()` validates required settings. `storage_class` and `source_provider` must be present, otherwise the
  session exits.
- `autouse_fixtures` (session, autouse) pulls in `source_provider_data`, `nfs_storage_profile`, `base_resource_name`,
  `forklift_pods_state`, and `virtctl_binary` for every run.
- `target_namespace` creates a unique OpenShift namespace for the run.
- `nfs_storage_profile` patches the NFS `StorageProfile` access modes and volume mode when the configured storage class is NFS.
- `forklift_pods_state` verifies the MTV operator `Subscription` exists and that every `forklift-*` pod in the MTV namespace is
  running or succeeded.
- `virtctl_binary` downloads and caches `virtctl` in a cluster-version-scoped shared directory under a file lock.
- `source_provider_data` resolves the selected source provider from `.providers.json` and fails fast if the key is missing.

The source provider setup is centered around `utilities/utils.py:create_source_provider()`. It picks the provider implementation
from the `type` field, creates the source `Secret`, creates the source `Provider` custom resource, waits until that `Provider` is
ready in Forklift, and only then opens the matching provider SDK wrapper.

```413:424:utilities/utils.py
    secret_string_data = {
        "url": source_provider_data_copy["api_url"],
        "insecureSkipVerify": "true" if insecure else "false",
    }
    provider_args = {
        "username": source_provider_data_copy["username"],
        "password": source_provider_data_copy["password"],
        "fixture_store": fixture_store,
    }
    metadata_labels = {
        "createdForProviderType": source_provider_data_copy["type"],
    }
```

```535:558:utilities/utils.py
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
        sdk_endpoint=source_provider_data_copy.get("endpoint_type"),
        annotations=provider_annotations or None,
    )
    # OVA (NFS-based) can transiently report ConnectionFailed while NFS initializes;
    # allow retry until timeout instead of failing immediately.
    stop_status = None if ova_provider(provider_data=source_provider_data_copy) else "ConnectionFailed"
    ocp_resource_provider.wait_for_status(Provider.Status.READY, timeout=600, stop_status=stop_status)

    # this is for communication with the provider
    with source_provider(ocp_resource=ocp_resource_provider, **provider_args) as _source_provider:
        if not _source_provider.test:
            pytest.fail(f"{source_provider.type} provider {provider_args['host']} is not available.")

        yield _source_provider
```

A few important details come from this design:

- The tests always create real Forklift `Provider` resources first. The provider SDK wrappers are helpers, not the system of
  record.
- The `sdk_endpoint` parameter is populated from `source_provider_data_copy.get("endpoint_type")` in the provider config. The name
  differs because `endpoint_type` is the user-facing config key (`"vcenter"` or `"esxi"`), while `sdk_endpoint` is the field name
  in the MTV `Provider` resource's settings (mapped to `sdkEndpoint`). This field is vSphere-only. When `endpoint_type` is missing
  or null, `sdk_endpoint` is passed as `None` and omitted from the Provider resource, and Forklift's mutating webhook then
  defaults it to `"vcenter"`.
- vSphere, RHV, OpenStack, OpenShift, Hyper-V, and OVA all share the same outer workflow, even though their provider-specific
  secrets and connection logic differ. RHV always fetches a CA certificate even when `insecureSkipVerify` is set, because imageio
  needs it.
- CA certificate placement is a parameter, not a second code path: `create_source_provider(ca_cert_key=...)` selects the secret
  data key, which is how `tests/cold/test_ca_crt_migration.py` exercises the `ca.crt` field.
- Copy-offload vSphere providers get the `forklift.konveyor.io/empty-vddk-init-image: "yes"` annotation and pass the
  `copyoffload` block into the provider wrapper.
- Remote OpenShift destination tests reuse the same basic pattern, but switch from `destination_provider` to
  `destination_ocp_provider`.

## 2. Forklift Inventory Discovery And Plan Preparation

Once the source `Provider` exists, `source_provider_inventory` resolves the matching `ForkliftInventory` subclass. This is the
layer that queries the `forklift-inventory` route and turns Forklift's discovered objects into storage and network mappings.

The class-scoped `prepared_plan` fixture is where the user-facing plan config becomes an actual migration input. It deep-copies
the config, validates and clones source VMs, resolves the PVC name template against the provider type, creates custom namespaces
if requested, adjusts source power state, stores source-side metadata, and creates hooks.

```1156:1169:conftest.py
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
```

Cloning runs in two phases. First every VM is cloned, powered to `source_vm_power`, described through `vm_dict()`, and recorded
in `plan["source_vms_data"]`. Then a single blocking wait covers all of them. `inventory_timeout` is one of the optional plan flags — it defaults to `300` seconds
when a config omits it. Other optional flags in the same fixture, such as `warm_migration`, are read the same way; only keys a plan must supply are read with direct indexing:

```1459:1467:conftest.py
            # Phase 2: wait for all cloned VMs in Forklift inventory after every clone completes.
            # Sequential per-VM wait during cloning causes inventory sync failures on VM2+.
            inventory_timeout = plan.get("inventory_timeout", 300)
            wait_for_cloned_vms_in_forklift_inventory(
                source_provider=source_provider,
                source_provider_inventory=source_provider_inventory,
                cloned_vm_names=cloned_vm_names,
                inventory_timeout=inventory_timeout,
            )
```

```1496:1498:conftest.py
    # Create Hooks if configured
    create_hook_if_configured(plan, "pre_hook", "pre", fixture_store, ocp_admin_client, target_namespace)
    create_hook_if_configured(plan, "post_hook", "post", fixture_store, ocp_admin_client, target_namespace)
```

This is one of the most important parts of the whole project. The test does not rush from "provider-side clone exists" to "create
the Plan." It waits until Forklift inventory has caught up.

Several plan flags shape what `prepared_plan` does before any MTV resource exists:

- `skip_clone: True` uses existing VMs instead of clones, for plan-readiness scenarios. It is rejected on providers that do not
  support it, and it is incompatible with `disable_drs_for_vms`, `clone_to_same_host`, `preserve_static_ips` outside Hyper-V,
  `migrate_shared_disks`, and `add_nic`. Original power states are captured and restored on teardown because those VMs are
  shared, real VMs.
- `migrate_shared_disks` (plan level or per VM) triggers `relink_shared_disks()` after cloning, because independent clones
  otherwise lose the shared-disk relationship.
- `add_nic` adds a NIC after the clone is synced, which then requires `wait_for_added_nics_in_forklift_inventory()` to force a
  refresh and block until the new NIC appears.
- `clone_to_same_host`, `pin_to_non_dedicated_host`, `disable_drs_for_vms`, and the per-VM `target_esxi_host` control ESXi
  placement and DRS behavior on vSphere clones.
- `clone_provider` in `.providers.json` routes cloning through a vCenter-backed `vcenter_clone_provider` when the source is a
  bare ESXi host that cannot run `CloneVM_Task`.
- OpenShift sources skip cloning entirely: `create_source_cnv_vms()` creates the source VMs and a dedicated Multus network in
  `source_vms_namespace`.

> **Note:** RHV/oVirt is the main exception to the normal "inventory first" story for networks. Those tests clone from templates,
> so network lookups go through `source_provider.get_vm_or_template_networks()` rather than Forklift inventory. OVA is the other
> exception: OVA VMs are not cloned and only get a unique `targetName`.
>
> **Note:** OpenStack inventory waits are stricter than a simple VM name lookup. The inventory adapter also waits until attached
> volumes and networks are queryable, because StorageMap and NetworkMap generation depend on that metadata.

## 3. StorageMap And NetworkMap

After `prepared_plan` is ready, the suite creates the two map resources that make the rest of the workflow possible.

For `StorageMap`, the standard path is:

- Ask Forklift inventory which source storages the selected VM or VMs use.
- Map each of those source storages to the configured destination `storage_class`.

For `NetworkMap`, the rule is deterministic and simple:

- The first source network maps to the destination pod network.
- Every additional source network maps to a class-scoped Multus network attachment definition.
- If the plan sets `multus_namespace`, those NADs can live outside the main test namespace.

That behavior comes from `utilities/utils.py:gen_network_map_list()` and the `multus_network_name` fixture in `conftest.py`:

```331:345:utilities/utils.py
    network_map_list: list[dict[str, dict[str, str]]] = []
    _destination_pod: dict[str, str] = {"type": "pod"}
    multus_counter = 1

    if per_nic_network_map:
        networks = get_per_nic_networks(
            source_provider=source_provider, source_provider_inventory=source_provider_inventory, vms=vms
        )
    else:
        networks = source_provider_inventory.vms_networks_mappings(vms=vms)

    for index, network in enumerate(networks):
        if pod_only or index == 0:
            # First network or pod_only mode → pod network
            _destination = _destination_pod
```

`multus_network_name` sizes the NAD set from the same data, so the count of created NADs matches the count of map entries:

```968:980:conftest.py
    if class_plan_config.get("per_nic_network_map", False):
        multus_count = max(
            0,
            len(
                get_per_nic_networks(
                    source_provider=source_provider, source_provider_inventory=source_provider_inventory, vms=vms
                )
            )
            - 1
            + extra_nics,
        )
    else:
        multus_count = max(0, len(networks) - 1)  # First network goes to pod, rest to multus
```

A subtle but important detail happens right before Plan creation: `utilities/utils.py:populate_vm_ids()` injects Forklift
inventory IDs into the VM list. The Plan is not built from names alone.

> **Tip:** If a VM has only one NIC, no extra NADs are created. The Multus path only appears for the second and later source
> networks. Set `per_nic_network_map: True` when a VM has two NICs on the same source network and you need both preserved.

## 4. Plan Creation

`utilities/mtv_migration.py:create_plan_resource()` is the assembly point where providers, maps, VM IDs, and optional plan
features become an MTV `Plan` custom resource.

```187:216:utilities/mtv_migration.py
def create_plan_resource(
    ocp_admin_client: DynamicClient,
    fixture_store: dict[str, Any],
    source_provider: BaseProvider,
    destination_provider: OCPProvider,
    storage_map: StorageMap,
    network_map: NetworkMap,
    virtual_machines_list: list[dict[str, Any]],
    target_namespace: str,
    warm_migration: bool = False,
    pre_hook_name: str | None = None,
    pre_hook_namespace: str | None = None,
    after_hook_name: str | None = None,
    after_hook_namespace: str | None = None,
    test_name: str | None = None,
    copyoffload: bool = False,
    preserve_static_ips: bool = False,
    pvc_name_template: str | None = None,
    pvc_name_template_use_generate_name: bool | None = None,
    target_node_selector: dict[str, str] | None = None,
    target_labels: dict[str, str] | None = None,
    target_affinity: dict[str, Any] | None = None,
    vm_target_namespace: str | None = None,
    migrate_shared_disks: bool | None = None,
    target_power_state: str | None = None,
    enable_nested_virtualization: bool | None = None,
    xfs_compatibility: bool = False,
    run_preflight_inspection: bool | None = None,
    rdm_as_lun: bool | None = None,
) -> Plan:
```

The excerpt above shows the signature only. The function itself carries a full docstring documenting every argument, the returned `Plan`, and the `ValueError`
cases it raises — read `utilities/mtv_migration.py:216-240` for that contract.

The two `Any` types are deliberate, not laziness:

- `fixture_store: dict[str, Any]` — the store is a heterogeneous session registry: tracked resources live under `fixture_store["teardown"]`, keyed by resource
  kind (`Namespace`, `VirtualMachine`, `Migration`, ...), whose values are resource descriptors built at runtime by `create_and_store_resource()`.
  One union type per kind would have to be imported by every caller for no gain.
- `target_affinity: dict[str, Any]` — Kubernetes affinity accepts node selector terms, pod affinity/anti-affinity terms, and their weights, so the value is a nested
  structure whose leaf types differ per provider. Pinning it to one concrete type would reject valid plans.

Everything the suite itself constructs — `virtual_machines_list: list[dict[str, Any]]`, `pvc_name_template`, `target_labels`, `target_node_selector` — is typed as
narrowly as its content allows.

```281:305:utilities/mtv_migration.py
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
        "pre_hook_name": pre_hook_name,
        "pre_hook_namespace": pre_hook_namespace,
        "after_hook_name": after_hook_name,
        "after_hook_namespace": after_hook_namespace,
```

Optional features are only added when requested, so an unset flag never lands on the `Plan` CR:

```307:341:utilities/mtv_migration.py
    if target_node_selector:
        plan_kwargs["target_node_selector"] = target_node_selector

    if target_labels:
        plan_kwargs["target_labels"] = target_labels

    if target_affinity:
        plan_kwargs["target_affinity"] = target_affinity

    if migrate_shared_disks is not None:
        plan_kwargs["migrate_shared_disks"] = migrate_shared_disks

    if enable_nested_virtualization is not None:
        plan_kwargs["enable_nested_virtualization"] = enable_nested_virtualization

    if rdm_as_lun is not None:
        plan_kwargs["rdm_as_lun"] = rdm_as_lun

    # Add copy-offload specific parameters if enabled
    if copyoffload:
        # Set PVC naming template for copy-offload migrations
        # The volume populator framework requires this to generate consistent PVC names
        # Note: generateName is enabled by default, so Kubernetes adds random suffix automatically
        plan_kwargs["pvc_name_template"] = "pvc"

    if xfs_compatibility:
        plan_kwargs["xfs_compatibility"] = xfs_compatibility

    if run_preflight_inspection is not None:
        plan_kwargs["run_preflight_inspection"] = run_preflight_inspection
```

A few things to notice:

- The `Plan` resource itself lives in the test namespace, but the VMs can still target a separate `vm_target_namespace`.
- Hooks, labels, affinity, target power state, PVC naming, static IP preservation, nested virtualization, XFS compatibility,
  shared-disk migration, RDM-as-LUN, and preflight inspection are all Plan-time features in this suite.
- Per-VM `migrate_shared_disks` in the config is renamed to the camelCase `migrateSharedDisks` API field here, and test-only VM
  keys are stripped, so the serialized VM list stays valid for the CR.
- Copy-offload reuses the same overall path, but changes how the storage map is built and forces a fixed `pvc_name_template`
  because the volume populator framework needs consistent PVC names.
- `run_preflight_inspection` is the Deep Inspection switch on warm migrations. `None` leaves it at the Forklift default
  (enabled); several warm configs set it to `False` to skip preflight inspection entirely.

## 5. Migration Execution

Migration execution is intentionally small in the test code: the heavy lifting is delegated to MTV.

`execute_migration()` creates a `Migration` CR that references the prepared `Plan`, then `wait_for_migration_complate()` polls the
Plan status until it becomes `Succeeded` or `Failed`.

Cold migrations create the `Migration` immediately. Warm migrations do two extra things:

- They use `precopy_interval_forkliftcontroller` to patch the `ForkliftController` precopy interval from `snapshots_interval`.
- They pass `get_cutover_value()` when creating the `Migration`, which schedules cutover using `mins_before_cutover` from
  `tests/tests_config/config.py`.

```python
def get_cutover_value(current_cutover: bool = False) -> datetime:
    """Return the timestamp to set as the warm migration cutover time.

    Args:
        current_cutover (bool): When True, cut over immediately and return the current
            UTC time. When False, return the current UTC time plus
            ``py_config["mins_before_cutover"]`` minutes.

    Returns:
        datetime: Timezone-aware UTC cutover timestamp.

    Raises:
        KeyError: If ``mins_before_cutover`` is missing from the test configuration.
    """
    datetime_utc = datetime.now(pytz.utc)
    if current_cutover:
        return datetime_utc

    return datetime_utc + timedelta(minutes=int(py_config["mins_before_cutover"]))
```

Copy-offload tests use `execute_copyoffload_migration()` instead, so the populate pod logs are captured while the migration polls.
Throttling tests pair that with `execute_migration_monitoring_populator_inflight()`.

> **Tip:** In this project, warm migration is not just `warm_migration=True` on the Plan. The test also sets a cutover timestamp
> on the `Migration` resource.

## 6. Post-Migration Validation

`utilities/post_migration.py:check_vms()` is the main validator. It re-reads the source VM and the destination VM through the
provider abstraction layer, runs a wide set of checks, collects all failures per VM, and only then fails the test.

Validation depends on the plan and provider, and includes:

- Power state, CPU, CPU features, VBS status, and memory checks.
- Network verification against the created `NetworkMap`, plus NIC name preservation for static-IP scenarios.
- Storage verification against the created `StorageMap`.
- PVC naming checks when `pvc_name_template` is set.
- VMware snapshot and serial preservation checks.
- Guest agent verification.
- SSH connectivity to the migrated guest through `python-rrmngmnt`.
- Static IP preservation for VMs migrated from vSphere or Hyper-V with guest tools running.
- Target node placement, labels, and affinity when the plan asks for them.
- LUKS encryption verification (`verify_luks_encryption()`) for encrypted guests.
- XFS v4 compatibility (`check_vm_command_output()` driven by the plan's `xfs_check` config).
- Shared-disk data verification for Linux and Windows guests.
- Copy-offload data integrity checks.

This is why `prepared_plan["source_vms_data"]` matters: it preserves source-side facts that are needed later for snapshot,
PVC-name, and static-IP comparisons.

## 7. Advanced Paths

The normal workflow stays the same, but a few features add important branches.

**Warm migration** changes execution timing. The Plan is warm, the source-side clone can have Change Block Tracking enabled, the
Forklift precopy interval is patched, and the `Migration` is created with a cutover timestamp.

**Hooks** are created during `prepared_plan` by `utilities/hooks.py:create_hook_if_configured()`. A plan can define `pre_hook` or
`post_hook` with either a predefined success or failure playbook or a custom base64-encoded Ansible playbook.
`tests/hooks/test_post_hook_retain_failed_vm.py` shows the intended behavior: a pre-hook failure can stop VM validation because the
migration never really happened, while a post-hook failure can still leave migrated VMs behind and therefore still runs
`check_vms()`.

**AAP hooks** are the external variant. `tests/hooks/conftest.py` deploys AWX via Helm, creates an OAuth2 API token and job
templates, points MTV at AWX, and the test then uses the resulting AAP `Hook` CRs.

**Plan lifecycle** is covered by `tests/plan_lifecycle/test_plan_archive_pvc_cleanup.py`. It deliberately fails the migration at the
post-hook, confirms at least one PVC or DataVolume still exists in the dedicated `vm_target_namespace`, archives the Plan and waits
for `Archived=True`, deletes the Plan and confirms its `Migration` disappears, then deletes the retained destination VM and polls
for up to 120 seconds until no DataVolumes or PVCs remain.

**LUKS encryption** is covered by `tests/luks/`. The `luks_vm_specs` fixture resolves passphrases (per-VM override, else provider
`luks_passphrase`), creates the Kubernetes Secrets, and injects them into the plan. `tests/luks/test_luks_cold_migration.py` also
contains a wrong-passphrase class that expects the migration to fail.

**XFS compatibility** is covered by `tests/cold/test_cold_migration_xfs.py` and `tests/warm/test_warm_migration_xfs.py`. Both set
`xfs_compatibility: True` on the Plan and then run `xfs_info` inside the migrated guest to validate the filesystem version.

**Shared disks** are covered by `tests/shared_disk/`. The Linux test mounts, writes, and reads a marker from both migrated VMs. The
Windows test first labels the shared NTFS volume on the source VM through the VMware Guest Operations API, then verifies
bidirectional access on the destination.

**Copy-offload** keeps the same outer sequence but changes the storage side. In that mode:

- The source provider is still vSphere.
- The storage secret comes from the plan's `copyoffload` configuration and optional environment-variable overrides.
- `get_storage_migration_map()` uses datastore IDs and `offloadPlugin` entries instead of the standard inventory-derived storage
  list, and can expand to secondary or non-XCOPY datastores for multi-datastore and fallback scenarios.
- `rdm_as_lun` maps RDM disks as LUN devices with a SCSI bus instead of the default virtio mapping.
- `pin_to_non_dedicated_host` pins every VM to an ESXi host outside `copyoffload.dedicated_migration_hosts`, so dedicated-host
  verification can never coincide with a VM's own host.
- ForkliftController in-flight limits are patched for the throttling tests, and `verify_populator_throttling()` checks `sourceHost`
  labels and `PopulatorThrottled` events.
- If the provider requests SSH-based ESXi cloning, the `copyoffload_ssh_key` fixture installs an SSH key before the migration and
  removes it afterward.

**Deep Inspection** has two shapes. `tests/deep_inspection/test_standalone_di.py` creates a `Conversion` CR directly, waits for
completion, and verifies the inspection results, then exercises cancel, cleanup, and rerun.
`tests/deep_inspection/test_plan_driven_di.py` runs a warm migration that is expected to fail because Deep Inspection concerns
block it, and then verifies those concerns.

**Operator upgrade** is covered by `tests/upgrade/test_upgrade_migration.py`. `tests/upgrade/conftest.py` creates the `StorageMap`,
`NetworkMap`, and `Plan` before the upgrade; the test then runs the MTV upgrade script, verifies the operator version and that the
Plan survived the upgrade, and only then migrates.

**Remote OpenShift destination** tests also reuse the same pattern. The difference is mostly in which destination provider fixture
they use, not in the rest of the migration flow. `tests/warm/conftest.py` adds a ClusterRole-based destination provider plus an SCC
binding for the warm ClusterRole scenarios.

## 8. Teardown And Failure Handling

Cleanup is just as structured as setup.

Almost every OpenShift-side resource is created through `utilities/resources.py:create_and_store_resource()`, which deploys the
resource and records it in `fixture_store["teardown"]`. Class-level cleanup removes migrated VMs early, and session-level cleanup
handles everything else.

```140:161:utilities/pytest_utils.py
def session_teardown(session_store: dict[str, Any]) -> None:
    LOGGER.info("Running teardown to delete all created resources")

    ocp_client = get_cluster_client()

    # When running in parallel (-n auto) `session_store` can be empty.
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
```

`teardown_resources()` collects leftovers per kind and fails the teardown when anything survives. Besides the resources the tests
created, it also handles the resources the migration created:

```179:197:utilities/pytest_utils.py
    migrations = session_teardown_resources.get(Migration.kind, [])
    plans = session_teardown_resources.get(Plan.kind, [])
    providers = session_teardown_resources.get(Provider.kind, [])
    hosts = session_teardown_resources.get(Host.kind, [])
    secrets = session_teardown_resources.get(Secret.kind, [])
    network_attachment_definitions = session_teardown_resources.get(NetworkAttachmentDefinition.kind, [])
    networkmaps = session_teardown_resources.get(NetworkMap.kind, [])
    namespaces = session_teardown_resources.get(Namespace.kind, [])
    storagemaps = session_teardown_resources.get(StorageMap.kind, [])
    conversions = session_teardown_resources.get(Conversion.kind, [])
    vmware_cloned_vms = session_teardown_resources.get(Provider.ProviderType.VSPHERE, [])
    openstack_cloned_vms = session_teardown_resources.get(Provider.ProviderType.OPENSTACK, [])
    rhv_cloned_vms = session_teardown_resources.get(Provider.ProviderType.RHV, [])
    hyperv_cloned_vms = session_teardown_resources.get(Provider.ProviderType.HYPERV, [])
    openstack_volume_snapshots = session_teardown_resources.get("VolumeSnapshot", [])

    # Resources that was created by running migration
    pods = session_teardown_resources.get(Pod.kind, [])
    virtual_machines = session_teardown_resources.get(VirtualMachine.kind, [])
```

Session teardown does more than delete a few CRs:

- It cancels still-running migrations.
- It archives Plans before deletion.
- It deletes tracked `Provider`, `Host`, `Secret`, `NetworkAttachmentDefinition`, `StorageMap`, `NetworkMap`, `Namespace`,
  `Migration`, `Plan`, `Conversion`, and `VirtualMachine` resources, plus pods and `VirtualMachine` resources created by the
  migration.
- It waits for `DataVolume`, `PVC`, and `PV` cleanup.
- It reconnects to source providers to delete cloned source-side VMs, snapshots, and OpenStack volume snapshots that were created
  for the test.

If `--skip-teardown` is set, the class and session cleanup paths intentionally leave resources behind.

> **Note:** Failure handling is broader than cleanup. When data collection is enabled, the session writes created resources to
> `<data-collector-path>/resources.json` (default `.data-collector`), and `pytest_exception_interact` triggers
> `oc adm must-gather` collection so you can inspect what MTV and the cluster were doing at the time of failure.
> Setup/call failures queue one gather per class-plan instance per worker. The `tryfirst` `pytest_runtest_teardown(item, nextitem)`
> hook collects at a class or class-plan parameter boundary, including `nextitem=None` on early exit, before default fixture finalization.
> Directories use percent-encoded class node IDs, class parameter indices and worker IDs. A `tryfirst` `pytest_runtest_protocol` wrapper
> clears stale `plan_resource` before setup on class-plan transitions, not between consecutive methods of the same plan. The boundary
> binds the current Plan or explicit None before finalizers and retains it for retries, gathering fully for None. Earlier method teardown
> failures stay pending until the boundary; failures first reported after boundary cleanup use that context but collect post-finalizer state.
> Collection attempts at most one successful gather per class-plan-worker; operational failures remain pending and artifacts are not guaranteed
> if retries fail. A standalone test gathers immediately, once on success. Session-end retries cannot recover a crashed worker's pending state.

## Automation And Dry Runs

The repository includes local automation, but it is deliberately conservative.

`pytest.ini` wires in `tests/tests_config/config.py`, enables strict markers, produces JUnit XML, and uses `loadscope` distribution.
`tox.toml` does not try to run a real migration. Instead, it runs `pytest --setup-plan` and `pytest --collect-only`, which are
useful for validating test discovery, parametrization, and fixture wiring without depending on a live MTV environment. Both are
recognized as dry-run modes by `utilities/pytest_utils.py:is_dry_run()`, which the session and collection hooks use to skip cluster
access and the AI analysis path.

The `mtv-api-tests` CLI wraps the same workflow from outside `pytest`:

- `mtv-api-tests generate` writes `.providers.json` and `mtv-api-tests-manifests.yaml` through an interactive wizard.
- `mtv-api-tests run --mode local` shells out to `uv run pytest`; `--mode job` applies the generated Job manifest with `oc`.

Both accept a category from `all`, `copyoffload`, `tier0`, `tier1`, `warm`, and `remote`.

Optional AI failure analysis (`--analyze-with-ai`) posts failing test cases to a rootcoz server and enriches `junit-report.xml`. It
stays disabled unless `ROOTCOZ_SERVER_URL` is set, and the model defaults come from `.rootcoz/settings.json`, which rootcoz reads.
Pytest never reads that file itself.

That split is a good way to think about the project as a whole:

- Configuration chooses the source provider and migration shape.
- Fixtures turn that configuration into discoverable Forklift and OpenShift resources.
- Tests create maps, Plans, and Migrations in a fixed order.
- Validation compares source-side and destination-side reality.
- Teardown removes everything the run created, both in the cluster and on the source side when needed.

If you keep that control loop in mind, the rest of the repository becomes much easier to navigate.
