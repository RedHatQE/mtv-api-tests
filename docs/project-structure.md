# Project Structure

`mtv-api-tests` is organized as an end-to-end `pytest` suite for Migration Toolkit for Virtualization (MTV). Instead of a conventional Python application layout such as `src/`, the repository is split into scenario files, shared fixtures, provider adapters, validation helpers, and repository automation/configuration files.

> **Note:** The main entrypoint is `pytest`, not a packaged application. Most of the reusable behavior lives in `conftest.py`, `utilities/`, and `libs/`, while the files under `tests/` mostly define scenarios and expectations.

## At A Glance

```text
mtv-api-tests/
├── tests/
│   ├── tests_config/config.py
│   ├── test_mtv_cold_migration.py
│   ├── test_mtv_warm_migration.py
│   ├── test_copyoffload_migration.py
│   ├── test_cold_migration_comprehensive.py
│   ├── test_warm_migration_comprehensive.py
│   └── test_post_hook_retain_failed_vm.py
├── utilities/
├── libs/
│   ├── base_provider.py
│   ├── forklift_inventory.py
│   └── providers/
├── exceptions/
├── docs/
│   └── copyoffload/
├── tools/
├── conftest.py
├── pyproject.toml
├── uv.lock
├── pytest.ini
├── tox.toml
├── Dockerfile
├── .providers.json.example
├── .pre-commit-config.yaml
├── renovate.json
├── .release-it.json
├── .coderabbit.yaml
├── .pr_agent.toml
├── .flake8
├── .markdownlint.yaml
├── jira.cfg.example
├── OWNERS
└── junit_report_example.xml
```

A useful way to read the repository is:

1. `pytest.ini` tells you how the suite is launched.
2. `tests/tests_config/config.py` tells you what each scenario wants to do.
3. `tests/*.py` tells you which migration flow is being exercised.
4. `conftest.py` shows how providers, namespaces, networks, hooks, and cleanup are prepared.
5. `libs/` shows how the suite talks to source and destination platforms.
6. `utilities/` shows how MTV resources are created and how migrated VMs are validated.

`pytest.ini` makes that structure explicit by wiring `pytest` to the scenario config file, JUnit output, strict markers, Jira integration, and `xdist` distribution:

```1:25:pytest.ini
[pytest]
testpaths = tests

addopts =
  -s
  -o log_cli=true
  -p no:logging
  --tc-file=tests/tests_config/config.py
  --tc-format=python
  --junit-xml=junit-report.xml
  --basetemp=/tmp/pytest
  --show-progress
  --strict-markers
  --jira
  --dist=loadscope

markers =
    tier0: Core functionality tests (smoke tests)
    remote: Remote cluster migration tests
    warm: Warm migration tests
    copyoffload: Copy-offload (XCOPY) tests
    incremental: marks tests as incremental (xfail on previous failure)
    min_mtv_version: mark test to require minimum MTV version (e.g., @pytest.mark.min_mtv_version("2.6.0"))

junit_logging = all
```

## `tests/`: Scenario Suites

The `tests/` directory contains the scenario definitions. These are mostly thin wrappers around shared fixtures and helpers. That keeps each suite readable while still allowing the repository to cover many migration variations.

The main suites are:

- `tests/test_mtv_cold_migration.py` covers the core cold migration path and remote OpenShift cold migration.
- `tests/test_mtv_warm_migration.py` covers warm migration, cutover handling, and remote warm migration.
- `tests/test_copyoffload_migration.py` is the largest suite and covers copy-offload/XCOPY behavior such as thin and thick disks, snapshots, multi-datastore scenarios, scale tests, naming edge cases, simultaneous plans, and mixed XCOPY/VDDK behavior.
- `tests/test_cold_migration_comprehensive.py` covers higher-level cold migration features such as static IP preservation, PVC naming templates, node selection, labels, affinity, and custom target namespaces.
- `tests/test_warm_migration_comprehensive.py` does the same for warm migration.
- `tests/test_post_hook_retain_failed_vm.py` verifies hook-aware behavior when a migration is expected to fail after a post-hook but the migrated VM should still be retained.

A representative test class from `tests/test_mtv_cold_migration.py` shows the standard pattern used across the suite:

```17:58:tests/test_mtv_cold_migration.py
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

    def test_create_storagemap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
    ):
        """Create StorageMap resource for migration."""
        vms = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            target_namespace=target_namespace,
            vms=vms,
        )
        assert self.storage_map, "StorageMap creation failed"
```

That five-step flow repeats throughout the repository:

1. Create `StorageMap`
2. Create `NetworkMap`
3. Create `Plan`
4. Execute migration
5. Validate migrated VMs

Scenario data lives in `tests/tests_config/config.py`. This file does much more than list VM names: it carries migration mode, target power state, hook behavior, PVC naming templates, labels, affinity rules, copy-offload flags, timeouts, and other per-scenario settings.

For example, the comprehensive warm and cold scenarios define advanced feature coverage directly in configuration:

```434:516:tests/tests_config/config.py
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
        "vm_target_namespace": "mtv-comprehensive-vms",
        "multus_namespace": "default",  # Cross-namespace NAD access
    },
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

> **Tip:** When you want to understand a scenario quickly, start with its entry in `tests/tests_config/config.py`, then read the matching class in `tests/`, and only then follow the shared helpers it imports.

## `conftest.py`: Shared Fixtures And Runtime Orchestration

`conftest.py` is the operational heart of the repository. It is where session-wide and class-wide fixtures build the real test environment.

Key responsibilities in `conftest.py` include:

- Creating the target namespace with the right labels in `target_namespace`
- Loading the requested provider entry from `.providers.json` in `source_provider_data`
- Creating source and destination `Provider` CRs in `source_provider` and `destination_provider`
- Resolving the right Forklift inventory implementation in `source_provider_inventory`
- Downloading and caching `virtctl` for the current cluster in `virtctl_binary`
- Creating class-scoped Multus `NetworkAttachmentDefinition` resources in `multus_network_name`
- Preparing cloned VMs, custom namespaces, and optional hooks in `prepared_plan`
- Managing post-migration SSH sessions in `vm_ssh_connections`
- Cleaning up migrated VMs after each class in `cleanup_migrated_vms`

A few practical details are worth calling out:

- `prepared_plan` is where source VMs are cloned or otherwise prepared before the MTV `Plan` is created.
- The fixture stores detailed source VM facts separately in `plan["source_vms_data"]`, so the serialized `virtual_machines` list stays clean for MTV resource creation.
- Hook resources are created from plan configuration before the test methods start running.
- `virtctl_binary` is cached by cluster version and protected by file locking, which makes parallel `pytest-xdist` runs safer.

> **Note:** Reusable logic is intentionally centralized here. If a test file looks surprisingly small, that is usually by design.

## `libs/`: Provider Adapters And Inventory Clients

The `libs/` directory is the platform abstraction layer.

`libs/base_provider.py` defines the common interface the rest of the suite expects. Each provider implementation can connect, test availability, return a normalized VM description via `vm_dict()`, clone VMs where needed, delete VMs, and expose provider-specific network information through a shared contract.

The provider implementations are:

- `libs/providers/vmware.py` for VMware vSphere; this is the most feature-heavy adapter and includes cloning, guest info handling, datastore logic, snapshot handling, copy-offload support, and ESXi-related behavior.
- `libs/providers/rhv.py` for RHV/oVirt.
- `libs/providers/openstack.py` for OpenStack.
- `libs/providers/openshift.py` for OpenShift Virtualization/KubeVirt; this adapter is especially important on the destination side because it inspects migrated `VirtualMachine` resources.
- `libs/providers/ova.py` for OVA-based scenarios.

`libs/forklift_inventory.py` is the second half of the abstraction. Instead of talking to source providers directly, some mapping logic needs the Forklift inventory service. This file wraps the `forklift-inventory` API and provides provider-specific inventory classes such as `VsphereForkliftInventory`, `OvirtForkliftInventory`, `OpenstackForliftinventory`, `OpenshiftForkliftInventory`, and `OvaForkliftInventory`.

The fixture below shows how the right inventory client is selected at runtime:

```1193:1214:conftest.py
@pytest.fixture(scope="session")
def source_provider_inventory(
    ocp_admin_client: DynamicClient, mtv_namespace: str, source_provider: BaseProvider
) -> ForkliftInventory:
    if not source_provider.ocp_resource:
        raise ValueError("source_provider.ocp_resource is not set")

    providers = {
        Provider.ProviderType.OVA: OvaForkliftInventory,
        Provider.ProviderType.RHV: OvirtForkliftInventory,
        Provider.ProviderType.VSPHERE: VsphereForkliftInventory,
        Provider.ProviderType.OPENSHIFT: OpenshiftForkliftInventory,
        Provider.ProviderType.OPENSTACK: OpenstackForliftinventory,
    }
    provider_instance = providers.get(source_provider.type)

    if not provider_instance:
        raise ValueError(f"Provider {source_provider.type} not implemented")

    return provider_instance(  # type: ignore
        client=ocp_admin_client, namespace=mtv_namespace, provider_name=source_provider.ocp_resource.name
    )
```

This split is important when reading the codebase:

- `libs/providers/*` talks to the source or destination platform itself.
- `libs/forklift_inventory.py` talks to the MTV/Forklift inventory API that MTV uses for discovery and mapping.

## `utilities/`: Shared Orchestration, Validation, And Diagnostics

The `utilities/` directory is where most of the suite’s real work happens. If `tests/` describes *what* to migrate, `utilities/` contains most of the code for *how* to set up, execute, verify, and clean up the migration.

The major modules are:

- `utilities/mtv_migration.py` builds `StorageMap`, `NetworkMap`, `Plan`, and `Migration` resources and waits for migration completion.
- `utilities/post_migration.py` performs post-migration validation for CPU, memory, storage, networking, guest agent state, SSH, snapshots, static IP preservation, node placement, labels, and affinity.
- `utilities/resources.py` centralizes resource creation and teardown tracking.
- `utilities/utils.py` loads provider configuration, creates provider secrets and `Provider` CRs, fetches CA certificates, and contains general cluster helpers.
- `utilities/hooks.py` creates MTV hook resources and validates expected hook-related failure behavior.
- `utilities/migration_utils.py` handles cutover timing, plan archiving, migration cancelation, and cleanup checks for DVs, PVCs, and PVs.
- `utilities/ssh_utils.py` provides post-migration SSH access to VMs through `virtctl port-forward`.
- `utilities/virtctl.py` downloads the correct `virtctl` binary from the cluster for the current OS and architecture.
- `utilities/worker_node_selection.py` picks worker nodes for placement-sensitive tests, using Prometheus metrics when available.
- `utilities/copyoffload_migration.py`, `utilities/copyoffload_constants.py`, and `utilities/esxi.py` contain copy-offload-specific behavior and credentials handling.
- `utilities/must_gather.py` collects diagnostics with `oc adm must-gather`.
- `utilities/pytest_utils.py` handles dry-run behavior, resource collection, session teardown, and optional AI analysis wiring.
- `utilities/naming.py` generates short unique resource names and sanitizes VM names for Kubernetes.
- `utilities/logger.py` configures queue-based logging so parallel workers can write to a single stream cleanly.

The shared resource creation helper is one of the simplest and most important building blocks in the repository:

```19:69:utilities/resources.py
def create_and_store_resource(
    client: "DynamicClient",
    fixture_store: dict[str, Any],
    resource: type[Resource],
    test_name: str | None = None,
    **kwargs: Any,
) -> Any:
    kwargs["client"] = client

    _resource_name = kwargs.get("name")
    _resource_dict = kwargs.get("kind_dict", {})
    _resource_yaml = kwargs.get("yaml_file")

    if _resource_yaml and _resource_dict:
        raise ValueError("Cannot specify both yaml_file and kind_dict")

    if not _resource_name:
        if _resource_yaml:
            with open(_resource_yaml) as fd:
                _resource_dict = yaml.safe_load(fd)

        _resource_name = _resource_dict.get("metadata", {}).get("name")

    if not _resource_name:
        _resource_name = generate_name_with_uuid(name=fixture_store["base_resource_name"])

        if resource.kind in (Migration.kind, Plan.kind):
            _resource_name = f"{_resource_name}-{'warm' if kwargs.get('warm_migration') else 'cold'}"

    if len(_resource_name) > 63:
        LOGGER.warning(f"'{_resource_name=}' is too long ({len(_resource_name)} > 63). Truncating.")
        _resource_name = _resource_name[-63:]

    kwargs["name"] = _resource_name

    _resource = resource(**kwargs)

    try:
        _resource.deploy(wait=True)
    except ConflictError:
        LOGGER.warning(f"{_resource.kind} {_resource_name} already exists, reusing it.")
        _resource.wait()

    LOGGER.info(f"Storing {_resource.kind} {_resource.name} in fixture store")
    _resource_dict = {"name": _resource.name, "namespace": _resource.namespace, "module": _resource.__module__}

    if test_name:
        _resource_dict["test_name"] = test_name

    fixture_store["teardown"].setdefault(_resource.kind, []).append(_resource_dict)

    return _resource
```

That helper is used by higher-level orchestration code in `utilities/mtv_migration.py`. One good example is storage mapping, where the code branches between standard inventory-based mapping and copy-offload-specific mapping:

```445:505:utilities/mtv_migration.py
    if datastore_id and offload_plugin_config:
        # Copy-offload migration mode
        datastores_to_map = [datastore_id]
        if secondary_datastore_id:
            datastores_to_map.append(secondary_datastore_id)
            LOGGER.info(f"Creating copy-offload storage map for primary and secondary datastores: {datastores_to_map}")
        else:
            LOGGER.info(f"Creating copy-offload storage map for primary datastore: {datastore_id}")

        # Create a storage map entry for each XCOPY-capable datastore
        for ds_id in datastores_to_map:
            destination_config = {
                "storageClass": target_storage_class,
            }

            # Add copy-offload specific destination settings
            if access_mode:
                destination_config["accessMode"] = access_mode
            if volume_mode:
                destination_config["volumeMode"] = volume_mode

            storage_map_list.append({
                "destination": destination_config,
                "source": {"id": ds_id},
                "offloadPlugin": offload_plugin_config,
            })
            LOGGER.info(f"Added storage map entry for datastore: {ds_id} with copy-offload")

        # Add non-XCOPY datastore mapping (with offload plugin for fallback)
        if non_xcopy_datastore_id:
            destination_config = {"storageClass": target_storage_class}
            if access_mode:
                destination_config["accessMode"] = access_mode
            if volume_mode:
                destination_config["volumeMode"] = volume_mode
            storage_map_list.append({
                "destination": destination_config,
                "source": {"id": non_xcopy_datastore_id},
                "offloadPlugin": offload_plugin_config,
            })
            LOGGER.info(f"Added non-XCOPY datastore mapping for: {non_xcopy_datastore_id} (with xcopy fallback)")
    else:
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

A few utility modules are especially helpful to know by name:

- `utilities/post_migration.py` is where the deep VM checks happen. If a migrated VM has the wrong CPU, memory, disks, networks, PVC names, serial number, labels, or affinity, the logic is usually here.
- `utilities/ssh_utils.py` reaches migrated VMs through `virtctl port-forward`, so validation does not depend on cluster nodes exposing guest SSH directly.
- `utilities/hooks.py` supports both predefined success/failure hook playbooks and custom base64-encoded playbooks.
- `utilities/must_gather.py` is what the suite uses when it needs richer failure diagnostics.
- `utilities/worker_node_selection.py` exists because some scenarios validate placement-sensitive features rather than only migration success.

> **Tip:** If you are tracing a failure after the MTV `Plan` is created, `utilities/mtv_migration.py` and `utilities/post_migration.py` are usually the next files to read.

## `exceptions/`: Centralized Domain Errors

Custom exceptions are centralized in `exceptions/exceptions.py`. This keeps migration-specific failures easy to recognize and avoids scattering project-specific error types across multiple modules.

Representative exceptions include:

- `MigrationPlanExecError` for plan execution failure or timeout
- `MigrationNotFoundError` and `MigrationStatusError` for missing or incomplete migration CR state
- `VmPipelineError` and `VmMigrationStepMismatchError` for hook or pipeline analysis problems
- `MissingProvidersFileError` for missing or empty `.providers.json`
- `InvalidVMNameError`, `VmCloneError`, `VmBadDatastoreError`, and `VmNotFoundError` for provider-side and VM-side failures
- `SessionTeardownError` for cleanup problems after the run

This file is small, but it matters because the rest of the repository uses these names to make failures easier to understand during investigation.

## `docs/` And `tools/`: User Docs And Recovery Helpers

The checked-in `docs/` tree is currently small and focused. Right now it contains `docs/copyoffload/how-to-run-copyoffload-tests.md`, which is a user-facing guide for setting up and running copy-offload scenarios.

The `tools/` directory currently contains `tools/clean_cluster.py`, a recovery helper that reads a recorded resource list and calls `.clean_up()` on the matching objects. This is useful when a test run was interrupted and left resources behind.

A few other support files are worth knowing about:

- `junit_report_example.xml` shows the JUnit-style output shape emitted by `pytest`
- `JOB_INSIGHT_PROMPT.md` contains instructions for automated job/failure analysis tooling
- `OWNERS` lists repository approvers and reviewers

> **Tip:** `tools/clean_cluster.py` pairs naturally with the resource tracking written by `utilities/pytest_utils.py`, which stores created resources in a JSON file when data collection is enabled.

## Configuration, Tooling, And Automation Files

The repository root also contains the files that make the suite installable, configurable, lintable, and reviewable.

The most important ones are:

- `pyproject.toml` defines the Python project metadata and dependencies. It requires Python `>=3.12, <3.14` and includes `pytest`, `pytest-xdist`, `pytest-testconfig`, provider SDKs, `openshift-python-wrapper`, `openshift-python-utilities`, and other supporting libraries.
- `uv.lock` locks the exact dependency set used by the repository.
- `pytest.ini` configures test discovery, runtime options, markers, JUnit output, and `pytest-testconfig`.
- `tests/tests_config/config.py` is the suite’s shared Python-based scenario configuration file.
- `.providers.json.example` shows the structure expected by `load_source_providers()` and `source_provider_data()` for source providers and copy-offload settings.
- `jira.cfg.example` shows the minimal format expected by the Jira integration enabled in `pytest.ini`.
- `tox.toml` defines lightweight local automation tasks such as `pytest --setup-plan`, `pytest --collect-only`, and unused-code scanning.
- `.pre-commit-config.yaml` configures local quality gates including `flake8`, `ruff`, `ruff-format`, `mypy`, `detect-secrets`, `gitleaks`, and `markdownlint-cli2`.
- `Dockerfile` builds a Fedora-based test image, copies `utilities/`, `tests/`, `libs/`, and `exceptions/`, runs `uv sync --locked`, and defaults to `uv run pytest --collect-only`.
- `.release-it.json` handles version bumping, tagging, pushing, and GitHub release creation.
- `renovate.json` handles automated dependency update PRs and weekly lock file maintenance.
- `.coderabbit.yaml` and `.pr_agent.toml` configure automated review behavior.
- `.flake8` and `.markdownlint.yaml` hold narrower lint settings for Python and Markdown.

The provider configuration example is especially important because most real test runs depend on it:

```16:44:.providers.json.example
  "vsphere-copy-offload": {
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
```

> **Warning:** `.providers.json.example` contains placeholder values and inline comments such as `# pragma: allowlist secret`. Those comments are useful inside this repository, but they are not valid JSON. Remove them when creating your real `.providers.json`.

> **Warning:** This repository is code-complete, but many scenarios only make sense with live OpenShift, MTV, and source-provider environments. A local checkout without cluster access and provider credentials will let you read the structure, but not exercise the full migration stack.

> **Note:** In this repository snapshot, there is no checked-in `.github/workflows` directory. CI/CD-adjacent behavior is expressed mostly through local tooling and repository-bot configuration files such as `tox.toml`, `.pre-commit-config.yaml`, `Dockerfile`, `.release-it.json`, `renovate.json`, `.coderabbit.yaml`, and `.pr_agent.toml`.

> **Tip:** For the fastest mental model of the repository, read `pytest.ini`, then the matching scenario in `tests/tests_config/config.py`, then the test module in `tests/`, then `conftest.py`, and finally the helper modules imported from `utilities/` and `libs/`.
