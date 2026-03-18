# Introduction

`mtv-api-tests` is an end-to-end validation suite for Migration Toolkit for Virtualization (MTV). It is built on `pytest`, but it is not a typical unit-test project: it connects to real source providers, creates real MTV custom resources on OpenShift, runs actual migrations, and then checks the migrated virtual machines on the destination cluster.

That makes it useful when you need to answer practical questions such as:

- Can MTV migrate this VM from my provider into OpenShift Virtualization?
- Do warm migrations still behave correctly after an MTV or cluster upgrade?
- Did advanced plan settings such as hooks, copy-offload, PVC naming, labels, affinity, or target placement actually take effect?

## What mtv-api-tests is for

This project is aimed at people who need confidence in real migration behavior, not just API-level validation:

- QE and release-validation teams qualifying MTV across supported migration paths
- Platform engineers testing migrations in their own OpenShift environments
- Storage, provider, and partner teams validating feature-specific scenarios such as copy-offload
- Operators who need proof that a migrated VM still behaves the way they expect after the move

> **Warning:** `mtv-api-tests` is not a mock-based local test harness. It expects a live OpenShift environment with MTV installed, real source-provider credentials, and real VMs or templates to migrate.

## What it validates

The repository covers the full MTV workflow, not just a single API call or resource:

| Area | What the suite covers |
| --- | --- |
| Source providers | vSphere, RHV/oVirt, OpenStack, OVA, and OpenShift source-provider flows |
| Destination | OpenShift Virtualization, including remote-cluster-style scenarios |
| Migration types | Cold migration, warm migration, copy-offload, hook-based flows, and comprehensive feature combinations |
| MTV resources | `Provider`, `StorageMap`, `NetworkMap`, `Plan`, `Hook`, and `Migration` custom resources |
| VM outcome checks | Power state, CPU, memory, network mapping, storage mapping, PVC naming, guest agent, SSH connectivity, static IP preservation, node placement, labels, and affinity |

Warm migration coverage is provider-aware. The warm test suite explicitly skips unsupported source types such as OpenStack, OpenShift, and OVA, so the test matrix follows the support rules encoded by the project itself.

> **Tip:** Start with the `tier0` scenarios such as `test_sanity_cold_mtv_migration` or `test_sanity_warm_mtv_migration`. They exercise the same MTV lifecycle as the larger suites, but with a smaller and easier-to-debug scope.

## How a migration is validated

A typical `mtv-api-tests` run follows the same five-step pattern used throughout the repository:

1. Load the selected source provider from `.providers.json`.
2. Create or connect the MTV `Provider` resources on OpenShift.
3. Build `StorageMap` and `NetworkMap` resources for the selected VMs.
4. Create a `Plan` and then a `Migration` custom resource.
5. Inspect the migrated VM on OpenShift and compare it to the source VM and expected plan settings.

The core cold-migration test shows that pattern directly:

```python
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

vms = [vm["name"] for vm in prepared_plan["virtual_machines"]]
self.__class__.network_map = get_network_migration_map(
    fixture_store=fixture_store,
    source_provider=source_provider,
    destination_provider=destination_provider,
    source_provider_inventory=source_provider_inventory,
    ocp_admin_client=ocp_admin_client,
    target_namespace=target_namespace,
    multus_network_name=multus_network_name,
    vms=vms,
)
assert self.network_map, "NetworkMap creation failed"

populate_vm_ids(prepared_plan, source_provider_inventory)

self.__class__.plan_resource = create_plan_resource(
    ocp_admin_client=ocp_admin_client,
    fixture_store=fixture_store,
    source_provider=source_provider,
    destination_provider=destination_provider,
    storage_map=self.storage_map,
    network_map=self.network_map,
    virtual_machines_list=prepared_plan["virtual_machines"],
    target_namespace=target_namespace,
    warm_migration=prepared_plan.get("warm_migration", False),
)
assert self.plan_resource, "Plan creation failed"

execute_migration(
    ocp_admin_client=ocp_admin_client,
    fixture_store=fixture_store,
    plan=self.plan_resource,
    target_namespace=target_namespace,
)

check_vms(
    plan=prepared_plan,
    source_provider=source_provider,
    destination_provider=destination_provider,
    network_map_resource=self.network_map,
    storage_map_resource=self.storage_map,
    source_provider_data=source_provider_data,
    source_vms_namespace=source_vms_namespace,
    source_provider_inventory=source_provider_inventory,
    vm_ssh_connections=vm_ssh_connections,
)
```

That same lifecycle appears across cold, warm, comprehensive, hook, remote, and copy-offload suites. What changes from test to test is the migration scenario and the validation expectations, not the basic MTV flow.

Under the hood, that flow stays grounded in real platform state:

- Source-provider adapters in `libs/providers/` connect to actual provider APIs.
- The `prepared_plan` fixture can clone source VMs, power them on or off, create hooks, and prepare extra namespaces or networks before migration begins.
- The `ForkliftInventory` helpers query the live `forklift-inventory` route and wait until provider, VM, storage, and network data are actually available before proceeding.

## How configuration works

`mtv-api-tests` separates environment configuration from migration-scenario configuration.

### Provider and environment configuration

Source-provider credentials and connection details are loaded from `.providers.json`. The example file shows the expected shape:

```jsonc
{
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
  }
}
```

The same example file also includes entries for `ovirt`, `openstack`, `openshift`, and `ova`, so the project can model more than one kind of source platform. For copy-offload scenarios, the example file adds a `copyoffload` section with storage-vendor and datastore settings.

> **Note:** `.providers.json.example` contains inline comments for documentation and secret-scanning rules. Your real `.providers.json` must be valid JSON without those comments.

Those provider entries do more than create MTV `Provider` resources. They also supply guest credentials used later for SSH-based validation of migrated VMs.

### Scenario configuration

Individual migration scenarios live in `tests/tests_config/config.py`. That file is effectively the catalog of what the project knows how to validate. A single scenario can switch advanced MTV features on and off:

```python
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
    "multus_namespace": "default",
    "pvc_name_template": '{{ .FileName | trimSuffix ".vmdk" | replace "_" "-" }}-{{.DiskIndex}}',
    "pvc_name_template_use_generate_name": True,
    "target_labels": {
        "mtv-comprehensive-test": None,
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

This is a good example of what makes `mtv-api-tests` more than a smoke suite. A scenario can describe not only which VM to migrate, but also which migration mode to use and what should still be true afterward.

The same configuration file also carries global test settings such as:

- `mtv_namespace = "openshift-mtv"`
- `target_namespace_prefix = "auto"`
- `snapshots_interval = 2`
- `plan_wait_timeout = 3600`

Those defaults tell you a lot about the intended environment: MTV is expected to be present on the cluster, namespaces are created per test session, warm-migration precopy timing is tunable, and migrations are expected to run long enough to justify an explicit timeout.

## Why this is real migration validation

A successful MTV `Plan` is not the same thing as a successful migration outcome. `mtv-api-tests` adds value because it keeps checking after the migration controller finishes.

The `check_vms()` logic in `utilities/post_migration.py` shows the kind of user-visible validation the suite performs:

```python
if vm_guest_agent:
    try:
        check_guest_agent(destination_vm=destination_vm)
    except Exception as exp:
        res[vm_name].append(f"check_guest_agent - {str(exp)}")

# SSH connectivity check - only when destination VM is powered on
if vm_ssh_connections and destination_vm.get("power_state") == "on":
    try:
        check_ssh_connectivity(
            vm_name=vm_name,
            vm_ssh_connections=vm_ssh_connections,
            source_provider_data=source_provider_data,
            source_vm_info=source_vm,
        )
    except Exception as exp:
        res[vm_name].append(f"check_ssh_connectivity - {str(exp)}")

    # Static IP preservation check - only for Windows VMs with static IPs migrated from VSPHERE
    source_vm_data = plan.get("source_vms_data", {}).get(vm["name"], {})

    if (
        source_vm_data
        and source_vm_data.get("win_os")
        and source_provider.type == Provider.ProviderType.VSPHERE
    ):
        try:
            check_static_ip_preservation(
                vm_name=vm_name,
                vm_ssh_connections=vm_ssh_connections,
                source_vm_data=source_vm_data,
                source_provider_data=source_provider_data,
            )
        except Exception as exp:
            res[vm_name].append(f"check_static_ip_preservation - {str(exp)}")

# Check node placement if configured
if plan.get("target_node_selector") and labeled_worker_node:
    try:
        check_vm_node_placement(
            destination_vm=destination_vm,
            expected_node=labeled_worker_node["node_name"],
        )
    except Exception as exp:
        res[vm_name].append(f"check_vm_node_placement - {str(exp)}")

# Check VM labels if configured
if plan.get("target_labels") and target_vm_labels:
    try:
        check_vm_labels(
            destination_vm=destination_vm,
            expected_labels=target_vm_labels["vm_labels"],
        )
    except Exception as exp:
        res[vm_name].append(f"check_vm_labels - {str(exp)}")

# Check affinity if configured
if plan.get("target_affinity"):
    try:
        check_vm_affinity(
            destination_vm=destination_vm,
            expected_affinity=plan["target_affinity"],
        )
    except Exception as exp:
        res[vm_name].append(f"check_vm_affinity - {str(exp)}")
```

That means a run can fail for the reasons users actually care about:

- The VM came up with the wrong power state
- Guest connectivity never returned
- Static IP preservation did not hold
- The VM landed on the wrong node
- Labels or affinity settings were not applied
- Storage or network mappings did not produce the expected result

The repository also includes feature-specific suites that go beyond basic migration success:

- Copy-offload tests validate vSphere shared-storage migrations using `vsphere-xcopy-volume-populator`
- Hook tests validate both expected success and expected failure paths for pre- and post-migration hooks
- Comprehensive tests validate PVC naming, target namespaces, affinity, labels, and node selectors
- Remote scenarios validate migrations where the destination is modeled as an explicit OpenShift provider

## Automation-friendly by design

Although these are real-environment tests, the project is structured to run cleanly in automation. The repository-wide `pytest.ini` configuration makes that clear:

```ini
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
```

In practice, that means:

- Scenario data is injected consistently from `tests/tests_config/config.py`
- Results are emitted in JUnit format for downstream reporting
- Marker usage is enforced
- The suite is prepared for class-scoped parallel execution
- Jira integration is part of the default test run

The repository also ships a `Dockerfile` that installs the project with `uv` and provides a repeatable containerized execution environment. That makes it easier to run the same validation flow across teams, clusters, or lab environments without rebuilding the toolchain by hand.

`mtv-api-tests` is best understood as a migration-confidence suite. If you need to know whether MTV can really move VMs from a supported source provider into OpenShift Virtualization, and whether the result still matches your expectations after the move, this project is built to answer that question.
