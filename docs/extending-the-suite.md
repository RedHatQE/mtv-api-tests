# Extending The Suite

Most new coverage in `mtv-api-tests` follows the same recipe:

1. Add a scenario to `tests/tests_config/config.py`.
2. Point a class at that scenario with `class_plan_config`.
3. Keep the standard five-step migration flow.
4. Reuse the shared fixtures for setup, cleanup, and validation.
5. Extend provider or validation helpers only when the existing abstractions stop being enough.

> **Note:** This suite is intentionally class-based. In most cases, adding a new test means adding one config entry and one new class, not building a brand-new setup stack.

## Add A Test Config

`pytest.ini` wires pytest-testconfig to `tests/tests_config/config.py`, so new scenarios start there.

A minimal cold-migration entry can be very small:

```python
    "test_sanity_cold_mtv_migration": {
        "virtual_machines": [
            {"name": "mtv-tests-rhel8", "guest_agent": True},
        ],
        "warm_migration": False,
    },
```

When you need more coverage, keep the same shape and add the keys the suite already understands:

```python
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
        "multus_namespace": "default",  # Cross-namespace NAD access
    },
```

A few patterns are worth knowing up front:

- `virtual_machines` is always the center of the scenario.
- `warm_migration` controls whether the flow is warm or cold.
- VM-level keys such as `source_vm_power`, `guest_agent`, `clone`, `disk_type`, `add_disks`, `snapshots`, and `clone_name` are already used by existing tests.
- Plan-level keys such as `target_power_state`, `preserve_static_ips`, `pvc_name_template`, `vm_target_namespace`, `target_node_selector`, `target_labels`, `target_affinity`, `pre_hook`, `post_hook`, and `copyoffload` are already supported by the shared helpers.

> **Tip:** In `target_node_selector` and `target_labels`, a value of `None` does not mean “missing”. The fixtures replace it with the current `session_uuid`, which makes it easy to create unique labels safely.

> **Note:** The runtime plan is not the raw config entry. `prepared_plan` deep-copies the config, clones VMs when needed, updates VM names, creates hooks, and stores extra source VM metadata. In test methods, always work from `prepared_plan`, not the literal values from `config.py`.

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

- `tests/test_mtv_cold_migration.py`
- `tests/test_mtv_warm_migration.py`
- `tests/test_cold_migration_comprehensive.py`
- `tests/test_warm_migration_comprehensive.py`
- `tests/test_copyoffload_migration.py`
- `tests/test_post_hook_retain_failed_vm.py`

The shared state also stays consistent: classes store `storage_map`, `network_map`, and `plan_resource` on the class itself so later steps can reuse them.

> **Warning:** Keep `@pytest.mark.incremental` on these classes. The steps depend on each other, and the suite is written to stop later steps cleanly when an earlier one fails.

When choosing markers, reuse the ones already declared in `pytest.ini`:

- `tier0` for core migration coverage
- `warm` for warm migration coverage
- `remote` for remote-cluster destination coverage
- `copyoffload` for XCOPY/copy-offload coverage
- `incremental` for dependent class flows

Warm classes also use `precopy_interval_forkliftcontroller`, and remote-destination classes switch from `destination_provider` to `destination_ocp_provider`.

## Reuse Fixtures Instead Of Rebuilding Setup

Most of the hard work is already in `conftest.py` and the utility modules. Reuse that layer first.

- `prepared_plan` is the main runtime plan fixture. It deep-copies the class config, prepares cloned VMs, tracks source VM metadata in `source_vms_data`, creates hooks when configured, and sets `_vm_target_namespace`.
- `target_namespace` creates a unique namespace for migration resources and stores it for cleanup.
- `source_provider` and `destination_provider` give you provider objects instead of raw credentials.
- `source_provider_inventory` gives you the Forklift inventory view that the mapping helpers use.
- `multus_network_name` automatically creates as many NetworkAttachmentDefinitions as the source VMs need and returns the base name and namespace that `get_network_migration_map()` expects.
- `cleanup_migrated_vms` deletes migrated VMs after the class finishes and automatically uses the custom VM namespace if your plan sets `vm_target_namespace`.
- `precopy_interval_forkliftcontroller` patches the `ForkliftController` for warm-migration snapshot timing, so warm tests should keep using it rather than patching the controller themselves.
- `labeled_worker_node` and `target_vm_labels` are the fixtures to use when your config includes `target_node_selector` or `target_labels`.
- `vm_ssh_connections` gives post-migration validation a reusable SSH connection manager.
- `copyoffload_config`, `copyoffload_storage_secret`, `setup_copyoffload_ssh`, and `mixed_datastore_config` are the copy-offload-specific fixtures already used by the XCOPY tests.
- `prepared_plan_1` and `prepared_plan_2` split a multi-VM plan into two independent plans for simultaneous migration coverage.

If you need to create an extra OpenShift resource for a new scenario, use `create_and_store_resource()` instead of deploying it directly. That helper generates a safe name when needed, deploys the resource, and registers it in the fixture store for teardown.

> **Tip:** `target_namespace` and `vm_target_namespace` are different things. `target_namespace` is where the migration resources live. `vm_target_namespace` is an optional plan setting that tells MTV to place the migrated VMs in a different namespace.

## Extend Provider Coverage

Most test classes are already provider-neutral because they work through `source_provider`, `destination_provider`, and `source_provider_inventory`. In practice, extending provider coverage usually means keeping the same five-step class and passing a few extra provider-specific arguments.

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
            access_mode="ReadWriteOnce",
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

If you need a brand-new provider type, there are two places where the provider/inventory pairing is wired together. One of them is `source_provider_inventory` in `conftest.py`:

```python
    providers = {
        Provider.ProviderType.OVA: OvaForkliftInventory,
        Provider.ProviderType.RHV: OvirtForkliftInventory,
        Provider.ProviderType.VSPHERE: VsphereForkliftInventory,
        Provider.ProviderType.OPENSHIFT: OpenshiftForkliftInventory,
        Provider.ProviderType.OPENSTACK: OpenstackForliftinventory,
    }
```

A new provider type needs all of the following:

1. A concrete `BaseProvider` implementation under `libs/providers/`.
2. A matching `ForkliftInventory` implementation in `libs/forklift_inventory.py`.
3. Registration in `utilities/utils.py:create_source_provider()` so the fixture layer can construct the provider from `.providers.json`.
4. Registration in `conftest.py:source_provider_inventory()` so the mapping helpers know how to query storage and network data.
5. A `vm_dict()` implementation that fills the fields the validators already expect, including CPU, memory, NICs, disks, power state, and any provider-specific metadata your checks need.

The active source provider is selected from `.providers.json` through `load_source_providers()`, so provider coverage should usually be added by configuration first. Only add a new provider implementation when the suite genuinely needs a new backend, not just a new scenario.

## Extend Validation Coverage

For most new test scenarios, the best place to add coverage is `utilities/post_migration.py`, not the `test_check_vms()` method itself.

`check_vms()` is the central post-migration validator. It already covers:

- power state
- CPU and memory
- network mapping
- storage mapping
- PVC naming templates
- snapshots
- serial preservation
- guest agent state
- SSH connectivity
- static IP preservation
- node placement
- VM labels
- VM affinity
- RHV-specific power-off behavior

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

> **Tip:** Negative-path tests should still keep the five-step flow. `tests/test_post_hook_retain_failed_vm.py` shows the pattern: wrap `execute_migration()` in `pytest.raises(MigrationPlanExecError)` when failure is expected, then decide whether `check_vms()` should still run based on where the failure happened.

## Validate And Collect Your New Tests

The repository does not include a checked-in GitHub Actions or GitLab pipeline file. The validation path that is checked into the repo is visible in `pytest.ini`, `tox.toml`, `Dockerfile`, and `.pre-commit-config.yaml`.

`tox.toml` already defines the first validation pass for new tests:

```toml
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

That leads to a practical workflow for new suite extensions:

- Run `uv run pytest --collect-only` first. It is also the default `CMD` in the `Dockerfile`, which makes test discovery a first-class check in this repo.
- Run `uv run pytest --setup-plan` or `tox -e pytest-check` to catch setup and collection issues before trying a full migration run.
- Run `pre-commit run --all-files` before you send changes out. The repo’s hooks include `flake8`, `ruff`, `ruff-format`, `mypy`, `detect-secrets`, `gitleaks`, and `markdownlint-cli2`.
- Keep using the existing markers unless you truly need a new one.

> **Warning:** `pytest.ini` enables `--strict-markers`. If you introduce a new marker and do not add it to `pytest.ini`, collection will fail.

> **Tip:** Start with collection and setup validation before a live run. This suite depends on real clusters, real providers, and real credentials, so the fastest feedback loop is usually `--collect-only`, `--setup-plan`, and pre-commit.
