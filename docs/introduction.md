# Introduction

`mtv-api-tests` is an end-to-end validation suite for Migration Toolkit for Virtualization (MTV). It is built on `pytest`, but it is not a typical unit-test project: it connects to
real source providers, creates real MTV custom resources on OpenShift, runs actual migrations, and then checks the migrated virtual machines on the destination cluster.

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

> **Warning:** `mtv-api-tests` is not a mock-based local test harness. It expects a live OpenShift environment with MTV installed, real source-provider credentials, and real VMs or
> templates to migrate.

## What it validates

The repository covers the full MTV workflow, not just a single API call or resource:

| Area | What the suite covers |
| --- | --- |
| Source providers | vSphere (vCenter and ESXi endpoints), RHV/oVirt, OpenStack, OVA, OpenShift, and Hyper-V source-provider flows |
| Destination | OpenShift Virtualization, including remote-cluster-style scenarios |
| Migration types | Cold, warm, and copy-offload migrations, plus shared-disk, deep-inspection, and operator upgrade flows |
| MTV resources | `Provider`, `StorageMap`, `NetworkMap`, `Plan`, `Hook`, `Migration`, and `Conversion` custom resources |
| VM outcome checks | Power state, CPU, memory, network and storage mapping, PVC naming, guest agent, SSH, static IP and NIC-name preservation, node placement, labels, affinity |

Coverage is provider-aware, and those rules live in the code, not in this page.
The collection hook in `conftest.py` decides which tests run for the configured source provider:

```python
warm_unsupported = (
    Provider.ProviderType.OPENSTACK,
    Provider.ProviderType.OPENSHIFT,
    Provider.ProviderType.OVA,
    Provider.ProviderType.HYPERV,
)
if source_provider_type in warm_unsupported:
    warm_skip = pytest.mark.skip(reason=f"{source_provider_type} warm migration is not supported.")
    for item in items:
        if "warm" in item.keywords:
            item.add_marker(warm_skip)
```

```python
if source_provider_type != Provider.ProviderType.VSPHERE:
    vsphere_only_skip = pytest.mark.skip(reason="Test is only applicable to vSphere source providers")
    for item in items:
        if any(kw in item.keywords for kw in ("copyoffload", "shared_disk", "deep_inspection", "aap", "luks")):
            item.add_marker(vsphere_only_skip)
```

In practice, warm migration is skipped for OpenStack, OpenShift, OVA, and Hyper-V sources, while copy-offload, shared-disk,
deep-inspection, AAP hook, and LUKS tests run only against vSphere.

> **Tip:** Start with the `tier0` scenarios such as `test_sanity_cold_mtv_migration` or `test_sanity_warm_mtv_migration`. They exercise the same MTV lifecycle as the larger suites,
> but with a smaller and easier-to-debug scope.

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
    per_nic_network_map=prepared_plan.get("per_nic_network_map", False),
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

That same lifecycle appears across cold, warm, comprehensive, hook, remote, copy-offload, shared-disk, LUKS, XFS,
deep-inspection, and upgrade suites. What changes from test to test is the migration scenario and the validation
expectations, not the basic MTV flow.

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
  "$schema": "https://raw.githubusercontent.com/RedHatQE/mtv-api-tests/main/providers_schema.json",
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
    "endpoint_type": "vcenter"
  }
}
```

The same example file also includes profiles for `ovirt`, `openstack`, `openshift`, `ova`, and `hyperv`, so the project can model more
than one kind of source platform. Two additional vSphere profiles show the provider variants the suite understands:

- `vsphere-copy-offload` adds a `copyoffload` section with storage-vendor, datastore, and optional ESXi SSH settings.
- `vsphere-esxi` sets `"endpoint_type": "esxi"` and points `clone_provider` at the vCenter profile that runs the clone
  operations, because a standalone ESXi host cannot clone on its own.

> **Note:** `.providers.json.example` contains inline comments for documentation and secret-scanning rules. Your real `.providers.json` must be valid JSON without those comments.

Those provider entries do more than create MTV `Provider` resources. They also supply guest credentials used later for SSH-based validation of migrated VMs.

### Scenario configuration

Individual migration scenarios live in `tests/tests_config/config.py`. That file is effectively the catalog of what the project knows how to validate. A single scenario can switch
advanced MTV features on and off:

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
    "enable_nested_virtualization": False,
    "vm_target_namespace": f"mtv-vms-warm-comprehensive-{uuid.uuid4().hex[:4]}",
    "multus_namespace": "default",  # Cross-namespace NAD access
    # Keys must be Provider.ProviderType string values (e.g. "vsphere") or "default".
    "pvc_name_template": {
        "vsphere": '{{ .FileName | trimSuffix ".vmdk" | replace "_" "-" }}-{{.DiskIndex}}',
        "default": '{{ .VmName | trunc 32 | trimSuffix "-" }}-{{ .VmName | trunc -4 }}-disk-{{.DiskIndex}}',
    },
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
    "guest_agent_timeout": 600,
},
```

This is a good example of what makes `mtv-api-tests` more than a smoke suite. A scenario can describe not only which VM to migrate, but also which migration mode to use and what
should still be true afterward.

The same configuration file also carries global test settings such as:

- `mtv_namespace = "openshift-mtv"`
- `target_namespace_prefix = "auto"`
- `snapshots_interval = 2`
- `mins_before_cutover = 5`
- `plan_wait_timeout = 3600`
- `remote_ocp_cluster = ""`, which gates the remote-cluster scenarios
- `insecure_verify_skip = "true"` and `source_provider_insecure_skip_verify = "false"`, which control TLS verification for the cluster and for the source provider

Those defaults tell you a lot about the intended environment: MTV is expected to be present on the cluster, namespaces are created per test session, warm-migration precopy timing
is tunable, and migrations are expected to run long enough to justify an explicit timeout.

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
if vm_ssh_connections is not None and destination_vm.get("power_state") == "on":
    try:
        check_ssh_connectivity(
            vm_name=destination_vm_name,
            vm_ssh_connections=vm_ssh_connections,
            source_provider_data=source_provider_data,
            source_vm_info=source_vm,
        )
    except Exception as exp:
        res[vm_name].append(f"check_ssh_connectivity - {str(exp)}")

    # Static IP preservation check - for VMs with preserve_static_ips enabled, migrated from a
    # provider in _STATIC_IP_PROVIDERS
    if source_vm_data and plan.get("preserve_static_ips") and source_provider.type in _STATIC_IP_PROVIDERS:
        try:
            check_static_ip_preservation(
                vm_name=destination_vm_name,
                vm_ssh_connections=vm_ssh_connections,
                source_vm_data=source_vm_data,
                source_provider_data=source_provider_data,
            )
        except Exception as exp:
            res[vm_name].append(f"check_static_ip_preservation - {str(exp)}")

    # NIC name preservation check - only when preserve_static_ips is set
    # (udev rules are generated by the same firstboot script as static IP preservation)
    # preserve_static_ips is an optional plan flag, so .get() is correct here: a config that omits
    # it simply does not request static IP preservation.
    if source_vm_data and plan.get("preserve_static_ips") and source_provider.type == Provider.ProviderType.VSPHERE:
        try:
            check_nic_name_preservation(
                source_vm_data=source_vm_data,
                destination_vm=destination_vm,
            )
        except (AssertionError, ValueError) as exp:
            res[vm_name].append(f"check_nic_name_preservation - {str(exp)}")

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

# Nested virtualization checks not applicable for OCP→OCP migrations
if plan.get("enable_nested_virtualization") is False and source_provider.type != Provider.ProviderType.OPENSHIFT:
    try:
        check_cpu_features(
            destination_vm=destination_vm,
            expected_features=_NESTED_VIRT_DISABLED_FEATURES,
        )
    except (AssertionError, ValueError) as exp:
        res[vm_name].append(f"check_cpu_features - {str(exp)}")
```

That means a run can fail for the reasons users actually care about:

- The VM came up with the wrong power state
- Guest connectivity never returned
- Static IP or NIC-name preservation did not hold
- The VM landed on the wrong node
- Labels or affinity settings were not applied
- PVC names did not match the configured template
- Nested-virtualization CPU features were exposed although the plan disabled them

The repository also includes feature-specific suites that go beyond basic migration success:

- Copy-offload tests validate vSphere shared-storage migrations using the `vsphere-xcopy-volume-populator` populator
- Hook tests cover a pre-hook that must succeed followed by a post-hook that must fail, plus AAP-based hooks
- Comprehensive tests validate PVC naming, target namespaces, affinity, labels, and node selectors
- Shared-disk tests validate `migrateSharedDisks` ownership between an owner VM and a consumer VM
- LUKS tests validate that disk encryption survives the migration, plus the expected failure path with a wrong passphrase
- XFS tests validate XFS v4 compatibility by running `xfs_info` inside the migrated guest
- Deep-inspection tests drive the `Conversion` CR lifecycle, both standalone and plan-driven
- Plan-lifecycle tests validate that archiving a failed plan cleans up the PVCs and DV resources it created
- Upgrade tests create migration resources, upgrade the MTV operator, and then migrate on the new version
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
  --show-progress
  --strict-markers
  --dist=loadscope
```

In practice, that means:

- Scenario data is injected consistently from `tests/tests_config/config.py`
- Results are emitted in JUnit format for downstream reporting, which is what CI systems such as Jenkins, GitLab CI, and GitHub Actions parse
- Marker usage is enforced, and an unknown marker is an error rather than a silent no-op
- The suite is prepared for class-scoped parallel execution, although you still need to pass `-n` to start xdist workers
- `pytest-jira` is a declared dependency, but `--jira` is not part of the default `addopts`; enable it explicitly when your run is tied to Jira issues

The repository also ships a `Dockerfile` that installs the project with `uv` and provides a repeatable containerized execution environment.
That makes it easier to run the same validation flow across teams, clusters, or lab environments without rebuilding the toolchain by hand.
The image defaults to a collection-only run, and failed tests can be enriched with AI analysis through `--analyze-with-ai` when a `rootcoz` server URL is available.

`mtv-api-tests` is best understood as a migration-confidence suite. If you need to know whether MTV can really move VMs from a supported source provider into OpenShift
Virtualization, and whether the result still matches your expectations after the move, this project is built to answer that question.
