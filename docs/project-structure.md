# Project Structure

`mtv-api-tests` is organized as an end-to-end `pytest` suite for Migration Toolkit for Virtualization (MTV). Instead of a
conventional Python application layout such as `src/`, the repository is split into scenario files, shared fixtures, provider
adapters, validation helpers, and repository automation/configuration files.

> **Note:** The main entrypoint is `pytest`, not a packaged application. Most of the reusable behavior lives in `conftest.py`,
> `utilities/`, and `libs/`, while the files under `tests/` mostly define scenarios and expectations. There is also a thin
> `mtv-api-tests` CLI (`cli/mtv_api_tests/`) that wraps setup and local/Job execution.

## At A Glance

```text
mtv-api-tests/
├── tests/
│   ├── tests_config/config.py
│   ├── cold/
│   ├── copyoffload/
│   ├── deep_inspection/
│   ├── hooks/
│   ├── luks/
│   ├── plan_lifecycle/
│   ├── shared_disk/
│   ├── upgrade/
│   └── warm/
├── cli/mtv_api_tests/
├── utilities/
├── libs/
│   ├── providers/
│   ├── base_provider.py
│   └── forklift_inventory.py
├── exceptions/
├── scripts/hooks/
├── tools/
├── guides/copyoffload/
├── docs/
├── conftest.py
├── pyproject.toml
├── uv.lock
├── pytest.ini
├── tox.toml
├── Dockerfile
├── providers_schema.json
├── .providers.json.example
├── .pre-commit-config.yaml
├── renovate.json
├── .release-it.json
├── .coderabbit.yaml
├── .pr_agent.toml
├── .flake8
├── .markdownlint.yaml
├── .rootcoz/settings.json
├── AGENTS.md
├── test_plan_schema.md
├── README.md
├── jira.cfg.example
├── OWNERS
└── junit_report_example.xml
```

Test files do **not** live directly under `tests/`. Every scenario sits in a feature subdirectory (`tests/<feature>/`), and the
`check-test-file-location` pre-commit hook enforces that placement:

```24:27:scripts/hooks/check_test_file_location.py
def _is_tests_root_test_file(path: Path) -> bool:
    """Return True if ``path`` is repo-root ``tests/test_*.py`` (no subdirectory).

    Only ``tests/`` at the repository root is considered. Nested trees such as
    `plugins/.../tests/test_*.py` are not flagged.
```

`AGENTS.md` states the same rule as a MUST: put all test files under `tests/<feature>/`, and never add `unit_tests/` or
standalone test scripts, because these are live end-to-end tests against MTV.

A useful way to read the repository is:

1. `pytest.ini` tells you how the suite is launched.
2. `tests/tests_config/config.py` tells you what each scenario wants to do.
3. `tests/<feature>/test_*.py` tells you which migration flow is being exercised.
4. `conftest.py` shows how providers, namespaces, networks, hooks, and cleanup are prepared.
5. `libs/` shows how the suite talks to source and destination platforms.
6. `utilities/` shows how MTV resources are created and how migrated VMs are validated.

`pytest.ini` makes that structure explicit by wiring `pytest` to the scenario config file, JUnit output, strict markers, and
`xdist` distribution:

```1:38:pytest.ini
[pytest]
testpaths = tests

addopts =
  -s
  -o log_cli=true
  -p no:logging
  --tc-file=tests/tests_config/config.py
  --tc-format=python
  --junit-xml=junit-report.xml
  --show-progress
  --strict-markers
  --dist=loadscope

markers =
    tier0: Core functionality tests (smoke tests)
    tier1: Extended functionality tests
    remote: Remote cluster migration tests
    warm: Warm migration tests
    copyoffload: Copy-offload (XCOPY) tests
    copyoffload_sanity: Copy-offload sanity tests - core scenarios for quick validation
    copyoffload_snapshots: Copy-offload snapshot tests (vSphere only)
    shared_disk: Shared disk migration tests (vSphere only)
    incremental: marks tests as incremental (xfail on previous failure)
    min_mtv_version: mark test to require minimum MTV version (e.g., @pytest.mark.min_mtv_version("2.6.0"))
    ova: OVA provider-specific tests
    vsphere: vSphere provider-specific tests
    esxi: ESXi provider-specific tests
    rhv: RHV provider-specific tests
    hyperv: Hyper-V provider-specific tests
    openstack: OpenStack provider-specific tests
    openshift: OpenShift provider-specific tests
    deep_inspection: Deep Inspection / Conversion CR tests (vSphere only)
    ca_crt: CA certificate field (ca.crt) provider secret tests
    aap: AAP (Ansible Automation Platform) hook integration tests (vSphere only)
    upgrade: MTV operator upgrade tests

junit_logging = all
```

> **Note:** `pytest.ini` does not pass `--jira`, even though `pytest-jira` is still a dependency in `pyproject.toml` and
> `jira.cfg.example` still exists. Treat Jira as an opt-in extra you enable yourself.

## `tests/`: Feature Suites

The `tests/` directory contains the scenario definitions, grouped by feature. Each feature directory has its own `conftest.py`
when the feature needs extra fixtures. These are mostly thin wrappers around shared fixtures and helpers, which keeps each suite
readable while still allowing the repository to cover many migration variations.

**`tests/cold/`** covers the core cold migration path, the comprehensive cold scenario, XFS compatibility, dual-NIC plan
validation, `ca.crt` secret handling, `insecureSkipVerify` handling, and OVA cold migration. Its `conftest.py` provides
alternative `source_provider` fixtures (`ca_crt_source_provider`, `insecure_source_provider`) plus matching inventory fixtures,
because those scenarios need a provider registered differently from the session default.

**`tests/warm/`** covers sanity warm migration, the comprehensive warm scenario, warm XFS, and warm migration with a
ClusterRole-based destination provider and an SCC binding. Its `conftest.py` provides `clusterrole_destination_ocp_provider`
and `forklift_scc_binding`.

**`tests/copyoffload/`** is by far the largest suite (~228 KB) and covers copy-offload/XCOPY behavior: thin and thick disks,
snapshots, multi-datastore and mixed-datastore scenarios, scale tests, RDM, populator and VM in-flight throttling, dedicated
migration hosts, naming edge cases, simultaneous plans, and mixed XCOPY/VDDK behavior. Its `conftest.py` validates the copy-offload
configuration and patches ForkliftController limits.

**`tests/shared_disk/`** covers shared-disk migration for Linux and Windows guests, including relink verification and Windows
volume labeling.

**`tests/hooks/`** covers post-hook expected-failure behavior and AAP/AWX-backed hook integration. Its `conftest.py` deploys AWX
via Helm, creates an API token and job templates, and wires MTV to use it.

**`tests/luks/`** covers LUKS-encrypted disk migration, including a wrong-passphrase failure case. Its `conftest.py` provides
`luks_vm_specs`.

**`tests/deep_inspection/`** covers plan-driven warm Deep Inspection and standalone `Conversion` CR Deep Inspection, including
cancel and rerun coverage.

**`tests/plan_lifecycle/`** covers Plan archive and delete after a failed migration, followed by PVC/DataVolume cleanup
verification.

**`tests/upgrade/`** covers migration across an MTV operator upgrade: maps and Plan are created pre-upgrade by fixtures, the
operator is upgraded, then the migration runs.

**`tests/tests_config/`** contains only `config.py`, the shared Python-based scenario configuration loaded by `pytest-testconfig`.

A representative test class from `tests/cold/test_mtv_cold_migration.py` shows the standard pattern used across the suite:

```19:66:tests/cold/test_mtv_cold_migration.py
@pytest.mark.vsphere
@pytest.mark.rhv
@pytest.mark.openstack
@pytest.mark.openshift
@pytest.mark.esxi
@pytest.mark.hyperv
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

1. Create `StorageMap` (`test_create_storagemap`)
2. Create `NetworkMap` (`test_create_networkmap`)
3. Create `Plan` (`test_create_plan`)
4. Execute migration (`test_migrate_vms`)
5. Validate migrated VMs (`test_check_vms`)

Feature suites insert or replace steps. The verified variants, all defined in `AGENTS.md` under "Key Patterns" and present in
`tests/`, are: the 4-step plan-readiness pattern, the 6-step plan-archive PVC cleanup pattern, the 6-step shared-disk (Linux)
pattern, the 7-step shared-disk (Windows) pattern, the 6/7/8-step copy-offload patterns, the 6-step LUKS pattern, and the 6-step
XFS pattern. `AGENTS.md` names the exact test method for each step, so the authoritative naming list lives there rather than
being duplicated here.

Scenario data lives in `tests/tests_config/config.py`. This file does much more than list VM names: it carries migration mode,
target power state, hook behavior, PVC naming templates, labels, affinity rules, copy-offload flags, timeouts, and other
per-scenario settings.

For example, the comprehensive cold scenario defines advanced feature coverage directly in configuration:

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

The complete plan-option vocabulary is documented in `AGENTS.md` under "Plan Configuration Options". It covers `warm_migration`,
`preserve_static_ips`, `copyoffload`, `xfs_compatibility`, `migrate_shared_disks`, `inventory_timeout`, `clone_to_same_host`,
`pin_to_non_dedicated_host`, `disable_drs_for_vms`, `per_nic_network_map`, `skip_clone`, and `rdm_as_lun`.

> **Tip:** When you want to understand a scenario quickly, start with its entry in `tests/tests_config/config.py`, then read the
> matching class in `tests/<feature>/`, and only then follow the shared helpers it imports.

## `conftest.py`: Shared Fixtures And Runtime Orchestration

`conftest.py` is the operational heart of the repository. It is where session-wide and class-wide fixtures build the real test
environment, and where the `pytest` hooks that gate the run live.

Key fixtures in `conftest.py` include:

- `autouse_fixtures` (session, autouse) pulls in `source_provider_data`, `nfs_storage_profile`, `base_resource_name`,
  `forklift_pods_state`, and `virtctl_binary` for every run.
- `target_namespace` creates a unique OpenShift namespace for the run, labeled for restricted pod security.
- `nfs_storage_profile` patches the NFS `StorageProfile` access modes and volume mode when the configured storage class is NFS.
- `source_providers` and `source_provider_data` load the providers JSON and resolve the entry named by `source_provider`.
- `source_provider` creates the source `Secret` and `Provider` CR and yields the provider SDK wrapper; `destination_provider`
  creates the local OpenShift destination `Provider`.
- `source_provider_inventory` resolves the right Forklift inventory implementation.
- `virtctl_binary` downloads and caches `virtctl` for the current cluster.
- `precopy_interval_forkliftcontroller` patches the `ForkliftController` precopy interval from `snapshots_interval`.
- `vcenter_clone_provider` builds a second vCenter-backed provider for ESXi sources that declare `clone_provider`, because ESXi
  cannot run `CloneVM_Task` itself.
- `multus_network_name` creates the class-scoped Multus `NetworkAttachmentDefinition` resources needed for secondary NICs.
- `validated_source_vms` fails fast when configured source VMs or templates do not exist, before any cloning happens.
- `prepared_plan` clones source VMs, adjusts power state, and creates hooks; `prepared_plan_1` and `prepared_plan_2` split it
  for simultaneous-migration scenarios.
- `mtv_version_checker` skips tests whose `min_mtv_version` marker is not satisfied.
- `labeled_worker_node` and `target_vm_labels` implement the `target_node_selector` and `target_labels` plan features.
- `vm_ssh_connections` manages post-migration SSH sessions to migrated VMs.
- `cleanup_migrated_vms` deletes migrated VMs after each test class.

A few practical details are worth calling out:

- `prepared_plan` deep-copies the plan config, so the `tests/tests_config/config.py` entry is never mutated. It stores detailed
  source VM facts in `plan["source_vms_data"]`, so the serialized `virtual_machines` list stays clean for MTV resource creation.
- Cloning uses a **two-phase** pattern: every VM is cloned first, then `wait_for_cloned_vms_in_forklift_inventory()` waits for
  all of them. Cloning VM2+ while VM1 inventory sync is still pending is what used to break inventory sync.
- For vSphere the inventory wait also blocks until inventory NIC MACs converge with live vCenter MACs, so the destination never
  gets a stale NIC MAC.
- `add_nic` reconfigures hardware *after* the clone was synced, so `wait_for_added_nics_in_forklift_inventory()` forces a
  provider refresh and blocks until each VM's inventory NIC count matches. Otherwise the added NIC is missing from the
  `NetworkMap` and Forklift drops it.
- Hook resources are created from plan configuration before the test methods start running.
- `virtctl_binary` is cached in a cluster-version-scoped shared directory under the system temp dir, guarded by a file lock, an
  ownership check, and a symlink check, which makes parallel `pytest-xdist` runs safer.

`pytest_collection_modifyitems` in `conftest.py` is where provider-type gating happens at collection time: warm tests are skipped
for OpenStack, OpenShift, OVA, and Hyper-V; `copyoffload`, `shared_disk`, `deep_inspection`, `aap`, and `luks` marked tests are
skipped for non-vSphere sources; and `ca_crt` tests are skipped for OpenShift and OVA sources.

> **Note:** Reusable logic is intentionally centralized here, and the `check-autouse-fixtures` pre-commit hook enforces that
> `autouse_fixtures` is the only fixture allowed to use `autouse=True`. If a test file looks surprisingly small, that is usually
> by design.

## `libs/`: Provider Adapters And Inventory Clients

The `libs/` directory is the platform abstraction layer.

`libs/base_provider.py` defines `BaseProvider`, the common interface the rest of the suite expects. Each provider implementation
can connect, test availability, return a normalized VM description via `vm_dict()`, clone VMs where needed, delete VMs, and
expose provider-specific network information through a shared contract.

The provider implementations are:

- `libs/providers/vmware.py` for VMware vSphere; this is by far the largest adapter and includes cloning, guest info handling,
  datastore logic, snapshot handling, shared-disk relinking, copy-offload support, NIC addition, and ESXi-related behavior.
- `libs/providers/rhv.py` for RHV/oVirt; clones from templates rather than VMs.
- `libs/providers/hyperv.py` for Microsoft Hyper-V.
- `libs/providers/openstack.py` for OpenStack.
- `libs/providers/openshift.py` for OpenShift Virtualization/KubeVirt; this adapter is especially important on the destination
  side because it inspects migrated `VirtualMachine` resources.
- `libs/providers/ova.py` for OVA-based scenarios.

`libs/forklift_inventory.py` is the second half of the abstraction. Instead of talking to source providers directly, some mapping
logic needs the Forklift inventory service. This file wraps the `forklift-inventory` route and provides the abstract
`ForkliftInventory` base plus provider-specific subclasses: `OvirtForkliftInventory`, `OpenstackForliftinventory`,
`VsphereForkliftInventory`, `OvaForkliftInventory`, `OpenshiftForkliftInventory`, and `HypervForkliftInventory`.

The fixture and the factory behind it select the right inventory client at runtime:

```1712:1717:conftest.py
@pytest.fixture(scope="session")
def source_provider_inventory(
    ocp_admin_client: DynamicClient, mtv_namespace: str, source_provider: BaseProvider
) -> ForkliftInventory:
    return create_forklift_inventory(client=ocp_admin_client, mtv_namespace=mtv_namespace, provider=source_provider)
```

```52:66:libs/forklift_inventory.py
    if not PROVIDER_INVENTORY_MAP:
        _register_inventory_classes()

    if provider.ocp_resource is None:
        raise ValueError(f"{provider.type} provider ocp_resource is not set")

    provider_class = PROVIDER_INVENTORY_MAP.get(provider.type)
    if provider_class is None:
        raise ValueError(f"Provider {provider.type} not implemented")

    return provider_class(  # type: ignore[call-arg]  # Subclasses use 'namespace' param while base class uses 'mtv_namespace'
        client=client,
        namespace=mtv_namespace,
        provider_name=provider.ocp_resource.name,
    )
```

This split is important when reading the codebase:

- `libs/providers/*` talks to the source or destination platform itself.
- `libs/forklift_inventory.py` talks to the MTV/Forklift inventory API that MTV uses for discovery and mapping.

## `utilities/`: Shared Orchestration, Validation, And Diagnostics

The `utilities/` directory is where most of the suite's real work happens. If `tests/` describes *what* to migrate, `utilities/`
contains most of the code for *how* to set up, execute, verify, and clean up the migration.

The major modules are:

- `utilities/mtv_migration.py` builds `StorageMap`, `NetworkMap`, `Plan`, and `Migration` resources, waits for migration
  completion, and resolves dual/simultaneous migration completion.
- `utilities/post_migration.py` performs post-migration validation for CPU and CPU features, VBS status, memory, storage,
  networking, guest agent state, SSH, snapshots, static IP preservation, node placement, labels, affinity, PVC names, data
  integrity, and LUKS encryption.
- `utilities/resources.py` centralizes resource creation (`create_and_store_resource`, `unregister_teardown_resource`,
  `get_or_create_namespace`) and teardown tracking.
- `utilities/utils.py` loads provider configuration, creates provider secrets and `Provider` CRs, fetches CA certificates,
  generates network map lists, populates inventory VM IDs, and contains general cluster helpers.
- `utilities/provider_inventory.py` owns Forklift inventory synchronization: forced provider refresh, added-NIC convergence,
  MAC convergence, cloned-VM waits, and pre-clone source VM validation.
- `utilities/hooks.py` creates MTV hook resources and validates expected hook-related failure behavior.
- `utilities/migration_utils.py` handles cutover timing, plan archiving, migration cancelation, and cleanup checks for DVs,
  PVCs, and PVs.
- `utilities/ssh_utils.py` provides post-migration SSH access to VMs through the `python-rrmngmnt` library.
- `utilities/virtctl.py` downloads the correct `virtctl` binary from the cluster for the current OS and architecture.
- `utilities/vmware_guest_operations.py` runs guest commands over the VMware Guest Operations API, used to detect guest NIC
  names and IP origins.
- `utilities/worker_node_selection.py` picks worker nodes for placement-sensitive tests, using Prometheus metrics when
  available.
- `utilities/shared_disk.py` mounts, labels, writes, and reads shared disks on both migrated guests for Linux and Windows.
- `utilities/deep_inspection.py` manages the Deep Inspection `Conversion` CR lifecycle: connection secrets, creation, phase
  waiting, cancel, snapshot handling, result verification, and concern capture.
- `utilities/aap.py` supports Ansible Automation Platform hook integration.
- `utilities/copyoffload_migration.py` holds copy-offload orchestration and XCOPY verification.
- `utilities/copyoffload_constants.py` holds the supported storage vendors and the Forklift in-flight limits.
- `utilities/copyoffload_datastore.py` resolves per-disk datastore IDs and formats the related error messages.
- `utilities/copyoffload_plan_secret.py` polls for the Forklift-created plan secret.
- `utilities/forklift_controller_populator.py` patches ForkliftController populator in-flight limits.
- `utilities/esxi.py` contains ESXi SSH credential handling for SSH-based ESXi cloning.
- `utilities/upgrade.py` runs the MTV operator upgrade script for the upgrade suite.
- `utilities/must_gather.py` collects diagnostics with `oc adm must-gather`.
- `utilities/pytest_utils.py` handles dry-run detection, resource collection, session teardown, and optional AI analysis wiring.
- `utilities/naming.py` generates short unique resource names and sanitizes VM names for Kubernetes.
- `utilities/logger.py` configures queue-based logging so parallel workers can write to a single stream cleanly.
- `utilities/constants.py` holds `MTV_OPERATOR_NAME`.

The shared resource creation helper is one of the simplest and most important building blocks in the repository:

```19:70:utilities/resources.py
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

That helper is used by higher-level orchestration code in `utilities/mtv_migration.py`. One good example is storage mapping,
where the code branches between standard inventory-based mapping and copy-offload-specific mapping driven by datastore IDs and
`offloadPlugin` entries. The same module also validates that the configured `pvc_name_template` is compatible with the source
provider before the `Plan` is built.

A few utility modules are especially helpful to know by name:

- `utilities/post_migration.py` is where the deep VM checks happen. If a migrated VM has the wrong CPU, memory, disks, networks,
  PVC names, serial number, labels, or affinity, the logic is usually here.
- `utilities/ssh_utils.py` reaches migrated VMs through the `python-rrmngmnt` library, so validation does not depend on cluster
  nodes exposing guest SSH directly.
- `utilities/hooks.py` supports both predefined success/failure hook playbooks and custom base64-encoded playbooks.
- `utilities/must_gather.py` is what the suite uses when it needs richer failure diagnostics.
- `utilities/worker_node_selection.py` exists because some scenarios validate placement-sensitive features rather than only
  migration success.
- `utilities/provider_inventory.py` is the place to look when a migration fails because Forklift had not caught up with the
  source VM yet.

> **Tip:** If you are tracing a failure after the MTV `Plan` is created, `utilities/mtv_migration.py` and
> `utilities/post_migration.py` are usually the next files to read.

## `exceptions/`: Centralized Domain Errors

Custom exceptions are centralized in `exceptions/exceptions.py`. This keeps migration-specific failures easy to recognize and
avoids scattering project-specific error types across multiple modules. The `check-exceptions-location` pre-commit hook enforces
the placement.

Representative exceptions include:

- `MigrationPlanExecError` for plan execution failure or timeout
- `MigrationNotFoundError` and `MigrationStatusError` for missing or incomplete migration CR state
- `VmPipelineError` and `VmMigrationStepMismatchError` for hook or pipeline analysis problems
- `MissingProvidersFileError` and `ProviderEmptyContentError` for missing or empty providers JSON
- `InvalidVMNameError`, `VmCloneError`, `VmBadDatastoreError`, `VmMissingVmxError`, and `VmNotFoundError` for provider-side and
  VM-side failures
- `SessionTeardownError` for cleanup problems after the run
- `ForkliftPodsNotRunningError`, `MtvOperatorNotInstalledError`, and `RemoteClusterAndLocalCluterNamesError` for cluster and
  operator readiness
- `MtvUpgradeError`, `GuestCommandError`, `SSHConnectionSetupError`, `PowerShellCommandError`, and `ConversionError` for the
  upgrade, guest-operations, SSH, and Deep Inspection paths

This file is small, but it matters because the rest of the repository uses these names to make failures easier to understand
during investigation.

## `cli/`, `scripts/`, And `tools/`

`cli/mtv_api_tests/` is a Typer application exposed as the `mtv-api-tests` console script (`cli.mtv_api_tests:app` in
`pyproject.toml`). It has two subcommands:

- `mtv-api-tests generate` walks through an interactive wizard that writes `.providers.json` and `mtv-api-tests-manifests.yaml`,
  with a `--category` option drawn from `TEST_CATEGORIES` (`all`, `copyoffload`, `tier0`, `tier1`, `warm`, `remote`).
- `mtv-api-tests run` runs the suite either locally (`uv run pytest`) or as an OpenShift Job (`oc apply`), with options for
  category, source and destination provider keys, storage class, a `-k` test filter, and a Job YAML path.

`scripts/hooks/` holds local AST-based pre-commit hooks that enforce selected `AGENTS.md` rules. They run from the `repo: local`
block of `.pre-commit-config.yaml`:

- `check-no-kubernetes-runtime`: no `kubernetes.*` imports at runtime outside the allowed exceptions import.
- `check-no-dynamicclient-construct`: no `DynamicClient()` construction; use `get_cluster_client()`.
- `check-no-except-exception`: no `except Exception` outside `pytest_*` hooks in `conftest.py`.
- `check-no-module-load-source-providers`: no module-level `load_source_providers` under `tests/`.
- `check-exceptions-location`: `Exception` subclasses live in `exceptions/exceptions.py`.
- `check-test-file-location`: no `test_*.py` directly under `tests/`.
- `check-autouse-fixtures`: only `autouse_fixtures` may use `autouse=True`.
- `check-no-runtimeerror`: no `raise RuntimeError` outside `pytest_*` hooks in `conftest.py`.

Legacy violations are grandfathered through a baseline ratchet: `scripts/hooks/baselines/<hook_id>.txt` holds
`path:sha256-prefix` fingerprints, currently for `check_exceptions_location`, `check_no_except_exception`,
`check_no_kubernetes_runtime`, and `check_no_runtimeerror`. `scripts/hooks/README.md` documents the matching rules, including
that each row grants exactly one suppression.

`tools/` holds operational helpers:

- `tools/clean_cluster.py` reads a recorded resource list (the `resources.json` written by the data collector) and calls
  `.clean_up()` on the matching objects. This is useful when a test run was interrupted and left resources behind.
- `tools/bm-dns-setup.sh` configures DNS for bare-metal environments.
- `tools/update-branches.sh` updates local branches.

`guides/copyoffload/how-to-run-copyoffload-tests.md` is the long-form user guide for setting up and running copy-offload
scenarios. It lives in `guides/`, not in `docs/`.

A few other support files are worth knowing about:

- `AGENTS.md` is the authoritative rules document for this repository: commands, code standards, architecture patterns, test
  structure, markers, and fixture rules.
- `test_plan_schema.md` defines the required class docstring (a manual product test plan) that every new test class must carry.
- `JOB_INSIGHT_PROMPT.md` contains instructions for automated job/failure analysis tooling.
- `OWNERS` lists repository approvers and reviewers.
- `.rootcoz/settings.json` configures the rootcoz analysis server, including the additional repositories it reads for this
  project: the `forklift` product repo, the `mtv-autodeploy` deployment repo, and the MTV Jenkins job repo.
- `junit_report_example.xml` shows the JUnit-style output shape emitted by `pytest`.

> **Tip:** `tools/clean_cluster.py` pairs naturally with the resource tracking written by `utilities/pytest_utils.py`, which
> stores created resources in a JSON file when data collection is enabled.

## Configuration, Tooling, And Automation Files

The repository root also contains the files that make the suite installable, configurable, lintable, and reviewable.

The most important ones are:

- `pyproject.toml` defines the Python project metadata and dependencies. It requires Python `>=3.12, <3.14` and includes
  `pytest`, `pytest-xdist`, `pytest-testconfig`, `pytest-harvest`, provider SDKs (`pyvmomi`, `ovirt-python-sdk`, `openstacksdk`),
  `openshift-python-wrapper`, `openshift-python-utilities`, `typer`, `python-rrmngmnt`, `pyhelper-utils`, and `py-go-template`.
- `uv.lock` locks the exact dependency set used by the repository.
- `pytest.ini` configures test discovery, runtime options, markers, JUnit output, and `pytest-testconfig`.
- `tests/tests_config/config.py` is the suite's shared Python-based scenario configuration file.
- `providers_schema.json` is the JSON Schema that `.providers.json` is validated against; `.providers.json.example` is the
  commented field reference.
- `.providers.json` is the real providers file. It is **gitignored**, so every developer supplies their own.
- `jira.cfg.example` shows the minimal format expected by the `pytest-jira` integration.
- `tox.toml` defines lightweight local automation: `pytest --setup-plan`, `pytest --collect-only`, and unused-code scanning via
  `pyutils-unusedcode`.
- `.pre-commit-config.yaml` configures local quality gates including `flake8`, `ruff`, `ruff-format`, `mypy`, `detect-secrets`,
  `gitleaks`, `markdownlint-cli2`, and the eight `scripts/hooks/` AGENTS-rules hooks.
- `Dockerfile` is a two-stage UBI 10 build: a builder stage that copies `cli`, `docs`, `utilities`, `tests`, `libs`, and
  `exceptions` and runs `uv sync --locked`, and a minimal runtime stage that defaults to `uv run pytest --collect-only`. It can
  optionally install pinned `openshift-python-wrapper` / `openshift-python-utilities` commits via build args.
- `.release-it.json` handles version bumping, tagging, pushing, and GitHub release creation.
- `renovate.json` handles automated dependency update PRs.
- `.coderabbit.yaml` and `.pr_agent.toml` configure automated review behavior.
- `.flake8` and `.markdownlint.yaml` hold narrower lint settings for Python and Markdown. The Markdown config sets a
  180-character line limit and allows only `details`, `summary`, and `strong` as inline HTML.
- `MTV-VERSION` records the pinned MTV version (`2.12`). No script in the repository currently reads it.

The provider configuration example is especially important because most real test runs depend on it:

```19:41:.providers.json.example
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
```

The same example also shows the ESXi profile, which is the shape most often missed:

```131:141:.providers.json.example
  "vsphere-esxi": {
    "type": "vsphere",
    "version": "<SERVER VERSION>",
    "fqdn": "ESXI HOST FQDN/IP",
    "api_url": "<ESXI HOST FQDN/IP>/sdk",
    "username": "USERNAME",
    "password": "PASSWORD",  # pragma: allowlist secret
    "endpoint_type": "esxi",
    "vddk_init_image": "<PATH TO VDDK INIT IMAGE>",
    "clone_provider": "vsphere"  # Name of vCenter provider to use for VM cloning operations
  },
```

- `endpoint_type` selects `"vcenter"` or `"esxi"`. It maps to the MTV `Provider` `sdkEndpoint` field.
- `clone_provider` names another entry in the same file (a vCenter) that performs `CloneVM_Task`, because a bare ESXi host
  cannot. The `vcenter_clone_provider` fixture in `conftest.py` builds it.
- `luks_passphrase` at provider level is the fallback for the per-VM `luks_passphrase` override used by `tests/luks/`.

> **Warning:** `.providers.json.example` contains placeholder values and inline comments such as `# pragma: allowlist secret`.
> Those comments are useful inside this repository, but they are not valid JSON, so the file does not parse with a standard JSON
> reader. Remove them when creating your real `.providers.json`.
>
> **Warning:** This repository is code-complete, but many scenarios only make sense with live OpenShift, MTV, and
> source-provider environments. A local checkout without cluster access and provider credentials will let you read the
> structure, but not exercise the full migration stack.
>
> **Note:** In this repository snapshot, there is no checked-in `.github/workflows` directory. CI-adjacent behavior is expressed
> mostly through local tooling and repository-bot configuration files such as `tox.toml`, `.pre-commit-config.yaml`, `Dockerfile`,
> `.release-it.json`, `renovate.json`, `.coderabbit.yaml`, `.pr_agent.toml`, and `.rootcoz/settings.json`.
>
> **Tip:** For the fastest mental model of the repository, read `pytest.ini`, then the matching scenario in
> `tests/tests_config/config.py`, then the test module in `tests/<feature>/`, then `conftest.py`, and finally the helper modules
> imported from `utilities/` and `libs/`.
