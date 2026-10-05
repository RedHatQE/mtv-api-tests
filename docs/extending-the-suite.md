# Extending The Suite

Most new coverage in `mtv-api-tests` follows the same recipe:

1. Put the test file in the feature subdirectory under `tests/<feature>/`.
2. Add a scenario to `tests/tests_config/config.py`.
3. Point a class at that scenario with `class_plan_config`.
4. Keep the standard five-step migration flow, or one of the documented feature patterns.
5. Write the full manual product test plan in the class docstring.
6. Reuse the shared fixtures for setup, cleanup, and validation.

> **Note:** This suite is intentionally class-based. In most cases, adding a new test means adding one config entry and one new class, not building a brand-new setup stack.

> **Warning:** Test modules are not allowed directly under `tests/`. `AGENTS.md` requires `tests/<feature>/`, and the `check-test-file-location` pre-commit hook fails any
`tests/test_*.py` at the root. New features get a new subdirectory with its own `conftest.py`, not another root-level file.

The existing feature subdirectories are:

| Directory | Feature |
| --- | --- |
| `tests/cold/` | Cold migrations: sanity, comprehensive, XFS, OVA, dual-NIC, CA cert |
| `tests/warm/` | Warm migrations: sanity, comprehensive, XFS, cluster roles |
| `tests/copyoffload/` | Copy-offload (XCOPY), snapshots, throttling, dedicated hosts, RDM |
| `tests/shared_disk/` | Shared-disk migrations, Linux and Windows |
| `tests/plan_lifecycle/` | Plan archive and PVC cleanup |
| `tests/hooks/` | Hook integration and expected-failure scenarios |
| `tests/luks/` | LUKS-encrypted disk migrations |
| `tests/deep_inspection/` | Deep Inspection and standalone Conversion CRs |
| `tests/upgrade/` | MTV operator upgrade runs |
| `tests/tests_config/` | Shared `config.py` and root config plumbing |

## Write The Class Docstring Test Plan

Every new test class must carry its complete, customer-readable manual product test plan in the class docstring. The schema is defined in `test_plan_schema.md` at the repository
root, and `AGENTS.md` makes it a review blocker.

A customer must be able to follow the plan by hand using an equivalent disposable VM and registered providers, without this repository's VM names, CLI, fixtures, or configuration
files. The docstring uses four sections in this order:

```python
class TestFeature:
    """[Product scenario in one sentence.]

    Purpose/Regression:
        [Behavior, regression, and chosen failure path.]

    Prerequisites:
        [Provider capabilities, disposable VM characteristics, network/storage,
        permissions, feature settings, and isolated environment.]

    Test plan:
        1. [Prepare resources and confirm source inventory visibility.]
        2. [Create mappings, hooks, and Plan; confirm readiness.]
        3. [Perform the action and observe product status or failed step.]
        4. [Confirm resources or effects exist before testing cleanup.]
        5. [Perform cleanup action and verify its status.]
        6. [Check final state and remove remaining test resources.]

    Expected result:
        [Observable pass criteria and product evidence to inspect on failure.]
    """
```

The rules that catch the most review comments:

- Describe the product, not the test runner. Do not mention fixture behavior, automation defects, or flags such as `--skip-teardown`.
- Every numbered step says what to do **and** what to observe, in the same order the test runs.
- Include evidence that the condition under test existed **before** cleanup, so a cleanup test cannot pass on a namespace the migration never touched.
- Final steps account for every created resource, including provider-side networks and retained VMs.
- Every statement must match an assertion the test actually makes.

> **Tip:** `test_plan_schema.md` also carries a pre-review checklist. Run through it before requesting review rather than after.

## Add A Test Config

`pytest.ini` wires pytest-testconfig to `tests/tests_config/config.py`, so new scenarios start there.

A cold-migration entry can be very small. This is the real sanity scenario from `tests/tests_config/config.py`:

```python
    "test_sanity_cold_mtv_migration": {
        "virtual_machines": [
            {"name": "mtv-tests-rhel8", "guest_agent": True, "add_nic": True, "add_nic_start_connected": False},
        ],
        "warm_migration": False,
        "per_nic_network_map": True,
    },
```

When you need more coverage, keep the same shape and add the keys the suite already understands. This is the comprehensive cold scenario:

```python
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

A few patterns are worth knowing up front:

- `virtual_machines` is always the center of the scenario.
- `warm_migration` controls whether the flow is warm or cold.
- VM-level keys such as `source_vm_power`, `guest_agent`, `clone`, `disk_type`, `add_nic`, `add_nic_start_connected`, `add_disks`, `snapshots`, and `clone_name` are already used by
existing tests.
- Plan-level keys such as `target_power_state`, `preserve_static_ips`, `pvc_name_template`, `vm_target_namespace`, `target_node_selector`, `target_labels`, `target_affinity`,
`pre_hook`, `post_hook`, and `copyoffload` are already supported by the shared helpers.

> **Tip:** In `target_node_selector` and `target_labels`, a value of `None` does not mean "missing". The fixtures replace it with the current `session_uuid`, which makes it easy to
create unique labels safely.

> **Note:** The runtime plan is not the raw config entry. `prepared_plan` deep-copies the config, clones VMs when needed, updates VM names, creates hooks, and stores extra source
VM metadata. In test methods, always work from `prepared_plan`, not the literal values from `config.py`.

## Follow The Five-Step Class Pattern

The standard migration classes all use the same shape. The cold sanity test is the simplest example:

```python
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
    """Cold migration test - sanity check."""

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan
```

From there, the class follows the same five steps every time:

1. `test_create_storagemap()` builds the `StorageMap` with `get_storage_migration_map()`.
2. `test_create_networkmap()` builds the `NetworkMap` with `get_network_migration_map()`.
3. `test_create_plan()` populates VM IDs and creates the MTV `Plan` with `create_plan_resource()`.
4. `test_migrate_vms()` starts the migration with `execute_migration()`.
5. `test_check_vms()` validates the result with `check_vms()`.

That pattern is consistent across:

- `tests/cold/test_mtv_cold_migration.py`
- `tests/warm/test_mtv_warm_migration.py`
- `tests/cold/test_cold_migration_comprehensive.py`
- `tests/warm/test_warm_migration_comprehensive.py`
- `tests/copyoffload/test_copyoffload_migration.py`
- `tests/hooks/test_post_hook_retain_failed_vm.py`

The shared state also stays consistent: classes store `storage_map`, `network_map`, and `plan_resource` on the class itself so later steps can reuse them.

> **Warning:** Keep `@pytest.mark.incremental` on these classes. The steps depend on each other, and the suite is written to stop later steps cleanly when an earlier one fails.

### Feature patterns

Most features still start from the five steps; they insert one verification step, or replace the migration step when the product behavior lives somewhere else. These are the
patterns the suite actually uses.

| Pattern | Steps | Used by |
| --- | --- | --- |
| 4-step plan readiness | `verify_<feature>` -> storagemap -> networkmap -> plan | `tests/cold/test_ca_crt_migration.py` |
| 5-step base | storagemap -> networkmap -> plan -> migrate -> check_vms | most cold and warm classes |
| 6-step plan archive PVC cleanup | storagemap -> networkmap -> plan -> migrate (expected fail) -> archive_and_delete -> verify_pvc_cleanup | `tests/plan_lifecycle/test_plan_archive_pvc_cleanup.py` |
| 6-step shared disk (Linux) | storagemap -> networkmap -> plan -> migrate -> verify_shared_disk_data -> check_vms | `tests/shared_disk/test_shared_disk_rhel_migration.py` |
| 7-step shared disk (Windows) | label_shared_disk -> storagemap -> networkmap -> plan -> migrate -> verify_shared_disk_data -> check_vms | `tests/shared_disk/test_shared_disk_windows_migration.py` |
| 6-step copy-offload | storagemap -> networkmap -> plan -> migrate -> check_xcopy_used -> check_vms | `tests/copyoffload/test_copyoffload_migration.py` |
| 7-step copy-offload throttling | storagemap -> networkmap -> plan -> migrate -> verify_populator_throttling -> check_xcopy_used -> check_vms | populator-inflight classes |
| 8-step copy-offload VM + populator throttling | storagemap -> networkmap -> plan -> migrate -> verify_vm_inflight_throttling -> verify_populator_throttling -> check_xcopy_used -> check_vms | MTV-777 combined classes |
| 7-step copy-offload dedicated host | storagemap -> networkmap -> plan -> migrate -> verify_dedicated_migration_host -> check_xcopy_used -> check_vms | dedicated-migration-host classes |
| 6-step LUKS | storagemap -> networkmap -> plan -> migrate -> verify_luks_encryption -> check_vms | `tests/luks/test_luks_cold_migration.py` |
| 6-step XFS | storagemap -> networkmap -> plan -> migrate -> verify_xfs_version -> check_vms | `tests/cold/test_cold_migration_xfs.py`, `tests/warm/test_warm_migration_xfs.py` |

A few details that matter when you reuse these:

- **Plan readiness.** When the feature under test is exercised during provider or plan creation, no migration runs. Plan readiness is the proof. `TestCaCrtColdMigration` has
`test_verify_ca_crt_secret`, then storagemap, networkmap, and plan, and nothing else.
- **Plan archive PVC cleanup.** The migration step expects `MigrationPlanExecError` and asserts leftover `DataVolume` and PVC objects exist before archiving. Then the Plan is
archived, verified to reach the `Archived` condition, deleted, and unregistered from teardown. There is no `test_check_vms` step. `test_verify_pvc_cleanup` must delete the
destination VM that the failure left behind before polling for orphans, because that VM owns a `DataVolume` and a `PVC` and would mask the result. Isolate the run with
`vm_target_namespace` in the plan config, not with a fixture that overwrites `prepared_plan["_vm_target_namespace"]`.
- **Shared disk.** Linux tests insert `test_verify_shared_disk_data` before `test_check_vms`, using `verify_shared_disk_data()` from `utilities/shared_disk.py`. Windows tests
additionally prepend `test_label_shared_disk`, which labels the shared NTFS volume on the source VM through the VMware Guest Operations API before migration, and verify with
`verify_shared_disk_data_windows()`.
- **Copy-offload.** `test_check_xcopy_used` validates the transfer mechanism, not the migrated VM, which gives much clearer failure diagnostics. Use
`execute_copyoffload_migration()` from `utilities/copyoffload_migration.py` for the standard path, `execute_migration_monitoring_populator_inflight()` when you also need
populator concurrency data, and the low-level `wait_for_copyoffload_plan_secret()` / `create_log_capture_callback()` / `wait_for_migration_complate()` helpers only when several
migrations run at once and need independent control.
- **Copy-offload dedicated hosts.** The dedicated-host pattern also sets the `pin_to_non_dedicated_host` plan flag, so a VM's own registered host can never coincide with a
dedicated host and produce a false pass.

> **Warning:** Do not wait for the plan secret inside `create_plan_resource()`. Forklift creates the plan populator secret when the migration starts, not when the Plan becomes
Ready.

> **Tip:** Keep verification steps focused on one concern. `AGENTS.md` also asks you to question any new `test_`-prefixed method whose only purpose is a precondition for other
steps; that logic usually belongs in a fixture.

When choosing markers, reuse the ones already declared in `pytest.ini`:

- `tier0` for core migration coverage
- `tier1` for extended functionality
- `warm` for warm migration coverage
- `remote` for remote-cluster destination coverage
- `copyoffload` for XCOPY/copy-offload coverage
- `copyoffload_sanity` and `copyoffload_snapshots` for copy-offload subsets
- `shared_disk` for shared-disk coverage
- `incremental` for dependent class flows

There are also provider markers that drive collection-time skipping: `vsphere`, `esxi`, `rhv`, `hyperv`, `openstack`, `openshift`, `ova`, `deep_inspection`, `ca_crt`, `aap`,
`upgrade`, and `min_mtv_version`. `pytest_collection_modifyitems` in `conftest.py` matches tests by marker keyword, so a test that should be skipped for the wrong provider is
skipped only if it carries the right marker.

Class names should carry the feature name for marker-gated features: warm migration classes contain `Warm`, copy-offload classes contain `Copyoffload`, snapshot classes contain
both `Copyoffload` and `Snapshot`, and tier1 classes include the feature name such as `TestLuksColdMigration`.

Warm classes also use `precopy_interval_forkliftcontroller`, and remote-destination classes switch from `destination_provider` to `destination_ocp_provider`.

## Reuse Fixtures Instead Of Rebuilding Setup

Most of the hard work is already in `conftest.py` and the utility modules. Reuse that layer first.

- `prepared_plan` is the main runtime plan fixture. It deep-copies the class config, prepares cloned VMs, tracks source VM metadata in `source_vms_data`, creates hooks when
configured, and sets `_vm_target_namespace`.
- `target_namespace` creates a unique namespace for migration resources and stores it for cleanup.
- `source_provider` and `destination_provider` give you provider objects instead of raw credentials.
- `source_provider_inventory` gives you the Forklift inventory view that the mapping helpers use.
- `multus_network_name` automatically creates as many NetworkAttachmentDefinitions as the source VMs need and returns the base name and namespace that `get_network_migration_map()`
expects.
- `cleanup_migrated_vms` deletes migrated VMs after the class finishes and automatically uses the custom VM namespace if your plan sets `vm_target_namespace`.
- `precopy_interval_forkliftcontroller` patches the `ForkliftController` for warm-migration snapshot timing, so warm tests should keep using it rather than patching the controller
themselves.
- `labeled_worker_node` and `target_vm_labels` are the fixtures to use when your config includes `target_node_selector` or `target_labels`.
- `vm_ssh_connections` gives post-migration validation a reusable SSH connection manager.
- `copyoffload_config`, `copyoffload_storage_secret`, `copyoffload_ssh_key`, and `mixed_datastore_config` are the copy-offload fixtures in `tests/copyoffload/conftest.py` used by
the XCOPY tests. Feature-specific fixtures belong in the feature's own `conftest.py`, not in the root one.
- `prepared_plan_1` and `prepared_plan_2` split a multi-VM plan into two independent plans for simultaneous migration coverage.

If you need to create an extra OpenShift resource for a new scenario, use `create_and_store_resource()` instead of deploying it directly. That helper generates a safe name when
needed, deploys the resource, and registers it in the fixture store for teardown.

```python
def create_and_store_resource(
    client: DynamicClient,
    fixture_store: dict[str, Any],
    resource: type[Resource],
    test_name: str | None = None,
    **kwargs: Any,
) -> Any:
```

The `Any` types are load-bearing, not placeholders:

- `fixture_store: dict[str, Any]` — the session store is a heterogeneous registry: tracked resources live under `fixture_store["teardown"]`, keyed by
  resource kind (`Namespace`, `VirtualMachine`, `Migration`, ...) with descriptors built at runtime by `create_and_store_resource()`, so one union type per kind would
  have to be imported by every caller.
- `**kwargs: Any` and `-> Any` — this helper is deliberately generic over `ocp_resources` classes: the kwargs are whatever `resource(**kwargs)` accepts, and the return value
  is that constructed resource, whose type is only known at the call site.

This is a `MUST` rule in `AGENTS.md`. If a test deliberately deletes a tracked resource mid-test, remove it from teardown with `unregister_teardown_resource()` from
`utilities/resources.py` so session cleanup does not chase a missing object.

### Fixture rules

Four rules from `AGENTS.md` are enforced by the pre-commit hooks or by review:

- **Only `autouse_fixtures` may use `autouse=True`.** The `check-autouse-fixtures` hook fails any other autouse fixture.
- **Fixture names are nouns.** `source_provider` and `copyoffload_config` are fine; `setup_provider` or `validate_copyoffload_config` are not.
- **Never request a fixture twice.** Pick either the test method parameter or `@pytest.mark.usefixtures()`, not both. `cleanup_migrated_vms` is the canonical usefixtures case,
because its return value is never used.
- **Validate in fixtures, not in test methods or utilities.** If a config value is required, create a fixture that fails fast when it is missing. This also keeps the "no defaults
for our config" rule intact: read `py_config` and `plan` keys with direct `config["key"]` access, and use `.get()` only for external provider data or genuinely optional feature
flags.

> **Tip:** `target_namespace` and `vm_target_namespace` are different things. `target_namespace` is where the migration resources live. `vm_target_namespace` is an optional plan
setting that tells MTV to place the migrated VMs in a different namespace. `prepared_plan` always sets `_vm_target_namespace`, so validation code should read that key.

## Extend Provider Coverage

Most test classes are already provider-neutral because they work through `source_provider`, `destination_provider`, and `source_provider_inventory`. In practice, extending provider
coverage usually means keeping the same five-step class and passing a few extra provider-specific arguments.

The copy-offload tests are a good example. They still use `get_storage_migration_map()`, but add provider-specific storage plugin data instead of rewriting the whole flow:

```python
        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            offload_plugin_config=offload_plugin_config,
            volume_mode="Block",
        )
```

That is the pattern to follow when you want to add provider-specific behavior:

- Keep the class structure the same.
- Keep using the shared map and plan helpers.
- Add only the extra provider inputs the helper already supports.

A few existing provider-specific patterns are already in the suite:

- Warm migration tests gate unsupported source providers at module level with `pytest.mark.skipif(...)`.
- Remote destination tests use `destination_ocp_provider` and skip when `remote_ocp_cluster` is not configured.
- Copy-offload tests layer extra fixtures on top of the standard class flow rather than creating a separate framework.

### Adding A New Provider Backend

If you need a brand-new provider type, there are two places where the provider/inventory pairing is wired together. One of them is the inventory map in
`libs/forklift_inventory.py`, populated lazily by `_register_inventory_classes()`:

```python
def _register_inventory_classes() -> None:
    """Populate PROVIDER_INVENTORY_MAP after all classes are defined."""
    PROVIDER_INVENTORY_MAP.update({
        Provider.ProviderType.OVA: OvaForkliftInventory,
        Provider.ProviderType.RHV: OvirtForkliftInventory,
        Provider.ProviderType.VSPHERE: VsphereForkliftInventory,
        Provider.ProviderType.OPENSHIFT: OpenshiftForkliftInventory,
        Provider.ProviderType.OPENSTACK: OpenstackForliftinventory,
        Provider.ProviderType.HYPERV: HypervForkliftInventory,
    })
```

The `source_provider_inventory` fixture in `conftest.py` is just a thin wrapper over `create_forklift_inventory()`:

```python
@pytest.fixture(scope="session")
def source_provider_inventory(
    ocp_admin_client: DynamicClient, mtv_namespace: str, source_provider: BaseProvider
) -> ForkliftInventory:
    """Build the Forklift inventory object for the configured source provider.

    Args:
        ocp_admin_client: OpenShift client used to read Forklift inventory CRs
        mtv_namespace: Namespace the MTV operator is installed in
        source_provider: Connected source provider adapter

    Returns:
        ForkliftInventory: The inventory implementation matching the provider type.
    """
    return create_forklift_inventory(client=ocp_admin_client, mtv_namespace=mtv_namespace, provider=source_provider)
```

A new provider type needs all of the following:

1. A concrete `BaseProvider` implementation under `libs/providers/`.
2. A matching `ForkliftInventory` implementation registered in `libs/forklift_inventory.py`.
3. Registration in `utilities/utils.py:create_source_provider()` so the fixture layer can construct the provider from `.providers.json` and register it as a Forklift `Provider` CR.
4. A `vm_dict()` implementation that fills the fields the validators already expect, including CPU, memory, NICs, disks, power state, and any provider-specific metadata your checks
need.
5. An entry in `providers_schema.json` **and** `.providers.json.example` if you add any new `.providers.json` property.

The active source provider is selected from `.providers.json` through `load_source_providers()`, so provider coverage should usually be added by configuration first. Only add a new
provider implementation when the suite genuinely needs a new backend, not just a new scenario.

> **Warning:** `load_source_providers()` must only be called from fixtures or pytest hooks, never at module level in a test file. Module-level code runs before pytest parses
`--providers-json`, so the argument is silently ignored. The `check-no-module-load-source-providers` hook fails module-level calls under `tests/`.

## Extend Validation Coverage

For most new test scenarios, the best place to add coverage is `utilities/post_migration.py`, not the `test_check_vms()` method itself.

`check_vms()` is the central post-migration validator. It already covers:

- provider SSL configuration for VMware, RHV, and OpenStack
- power state
- guest agent state
- SSH connectivity
- static IP preservation
- NIC name preservation and disconnected-NIC state
- node placement
- VM labels
- VM affinity
- CPU and CPU features, and VBS status
- memory
- network mapping
- storage mapping and PVC naming templates
- snapshots
- serial preservation
- boot configuration
- RHV-specific false-VM-power-off behavior

The existing label, node-placement, and affinity checks show the pattern clearly:

```python
        if plan.get("target_node_selector") and labeled_worker_node:
            try:
                check_vm_node_placement(
                    destination_vm=destination_vm,
                    expected_node=labeled_worker_node["node_name"],
                )
            except Exception as exp:
                res[vm_name].append(f"check_vm_node_placement - {str(exp)}")

        if plan.get("target_labels") and target_vm_labels:
            try:
                check_vm_labels(
                    destination_vm=destination_vm,
                    expected_labels=target_vm_labels["vm_labels"],
                )
            except Exception as exp:
                res[vm_name].append(f"check_vm_labels - {str(exp)}")

        if plan.get("target_affinity"):
            try:
                check_vm_affinity(
                    destination_vm=destination_vm,
                    expected_affinity=plan["target_affinity"],
                )
            except Exception as exp:
                res[vm_name].append(f"check_vm_affinity - {str(exp)}")
```

When you want to add a new validation, the usual path is:

1. Add a plan key to `tests/tests_config/config.py` if the validation is scenario-driven.
2. Collect any setup-time data in `prepared_plan` or a dedicated fixture.
3. Pass any plan-level MTV fields through `create_plan_resource()` if the validation depends on plan configuration.
4. Add a focused helper such as `check_vm_labels()` or `check_pvc_names()` to `utilities/post_migration.py`.
5. Call that helper from `check_vms()` behind an `if plan.get("your_key"):` guard.

This keeps the test classes simple. The class still ends with `check_vms()`, and the validation logic stays in one place.

> **Tip:** Negative-path tests should still keep the five-step flow. `tests/hooks/test_post_hook_retain_failed_vm.py` shows the pattern: wrap `execute_migration()` in
`pytest.raises(MigrationPlanExecError)` when failure is expected, then decide whether `check_vms()` should still run based on where the failure happened.

## Raise Specific Exceptions

`AGENTS.md` requires specific exception types, and three pre-commit hooks enforce it:

- `check-exceptions-location` fails any `Exception` subclass defined outside `exceptions/exceptions.py`.
- `check-no-except-exception` fails `except Exception` outside pytest hooks in `conftest.py`.
- `check-no-runtimeerror` fails `raise RuntimeError` outside pytest hooks in `conftest.py`.

For a new domain error, add the class to `exceptions/exceptions.py` and import it:

```python
from exceptions.exceptions import MigrationTimeoutError, ProviderConnectionError

if not migration.wait_for_completion(timeout=3600):
    raise MigrationTimeoutError(f"Migration '{migration.name}' timed out after 1 hour")
```

For ordinary cases use built-ins: `ValueError` for bad input or config, `TypeError` for type problems, `KeyError` for missing keys (let it propagate), `ConnectionError` for
connection failures.

> **Warning:** `RuntimeError` is allowed only inside pytest hooks for infrastructure failures such as an unreachable cluster. `except Exception` is never the right recovery: it
hides `TypeError`, `AttributeError`, and `KeyError` bugs that should crash loudly.

> **Tip:** `check_no_kubernetes_runtime.py` also bans runtime `kubernetes.*` imports outside `TYPE_CHECKING`, with a narrow exception for `kubernetes.dynamic.exceptions`. Import
`DynamicClient` under `TYPE_CHECKING` and get real clients through `get_client()`. Constructing `DynamicClient()` directly is a separate hook failure,
`check-no-dynamicclient-construct`.

## Validate And Collect Your New Tests

The repository does not include a checked-in GitHub Actions or GitLab pipeline file. The validation path that is checked into the repo is visible in `pytest.ini`, `tox.toml`,
`Dockerfile`, and `.pre-commit-config.yaml`.

`tox.toml` already defines the first validation pass for new tests:

```toml
[env.pytest-check]
description = "Run pytest collect-only and setup-plan"
deps = ["uv"]
commands = [
  ["uv", "run", "pytest", "--setup-plan"],
  ["uv", "run", "pytest", "--collect-only"],
]
```

That leads to a practical workflow for new suite extensions:

- Run `uv run pytest --collect-only` first. It is also the default `CMD` in the `Dockerfile`, which makes test discovery a first-class check in this repo.
- Run `uv run pytest --setup-plan` or `tox -e pytest-check` to catch setup and collection issues before trying a full migration run.
- Run `pre-commit run --all-files` before you send changes out. Besides `flake8`, `ruff`, `ruff-format`, `mypy`, `detect-secrets`, `gitleaks`, and `markdownlint-cli2`, the config
includes eight local `repo: local` hooks in `scripts/hooks/` that enforce the AGENTS.md rules: `check-no-kubernetes-runtime`, `check-no-dynamicclient-construct`,
`check-no-except-exception`, `check-no-module-load-source-providers`, `check-exceptions-location`, `check-test-file-location`, `check-autouse-fixtures`, and
`check-no-runtimeerror`. See the [development workflow](development-workflow.html) page for what each one bans.
- Keep using the existing markers unless you truly need a new one.

> **Warning:** `pytest.ini` enables `--strict-markers`. If you introduce a new marker and do not add it to `pytest.ini`, collection will fail.

> **Warning:** `AGENTS.md` forbids running `pytest` or `uv run pytest` directly. These tests need live clusters, provider connections, and credentials. `--collect-only` and
`--setup-plan` are the dry-run modes the repository itself treats as safe; `is_dry_run()` in `utilities/pytest_utils.py` returns true for exactly those two flags and skips the
required-config check and all teardown.

> **Tip:** Start with collection and setup validation before a live run. This suite depends on real clusters, real providers, and real credentials, so the fastest feedback loop is
usually `--collect-only`, `--setup-plan`, and pre-commit.
