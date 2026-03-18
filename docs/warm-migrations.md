# Warm Migrations

Warm migrations in `mtv-api-tests` are the powered-on migration path. The suite starts from a running source VM, lets MTV perform one or more precopy rounds, and then completes the migration at a scheduled cutover time. If you are setting up or tuning warm coverage, the main knobs are `warm_migration`, `snapshots_interval`, and `mins_before_cutover`.

## What You Need To Set

A warm scenario in this repository is driven by a small set of plan fields:

- `warm_migration: True` tells the suite to create a warm MTV plan.
- `source_vm_power: "on"` makes the source VM run before migration starts.
- `guest_agent: True` enables guest-agent validation after cutover.
- `target_power_state` lets you enforce the expected destination VM power state.

Example from `tests/tests_config/config.py`:

```python
"test_sanity_warm_mtv_migration": {
    "virtual_machines": [
        {
            "name": "mtv-tests-rhel8",
            "source_vm_power": "on",
            "guest_agent": True,
        },
    ],
    "warm_migration": True,
},
```

In practice, warm tests in this repository also clone the source VM before migration. That makes repeated test runs safer and gives the suite a place to apply warm-specific preparation.

## How Warm Source Preparation Works

For vSphere sources, the suite explicitly enables Change Block Tracking (CBT) on the cloned VM before warm migration starts:

```python
# Enable Change Block Tracking (CBT) only for warm migrations
enable_ctk = kwargs.get("enable_ctk", False)
if enable_ctk:
    LOGGER.info("Enabling Change Block Tracking (CBT) for warm migration")
    cbt_option = vim.option.OptionValue()
    cbt_option.key = "ctkEnabled"
    cbt_option.value = "true"
```

That matters because warm migration depends on change tracking between precopy rounds.

> **Tip:** Auto-generated Plan and Migration names in this suite get a `-warm` suffix for warm runs, which makes cluster-side debugging easier when you inspect resources during a test session.

## Precopy Interval Tuning

Warm timing knobs live in `tests/tests_config/config.py`, which is the default test configuration file loaded by `pytest.ini`:

```python
snapshots_interval: int = 2
mins_before_cutover: int = 5
plan_wait_timeout: int = 3600
```

The `snapshots_interval` value is applied through the `precopy_interval_forkliftcontroller` fixture, which patches the live `ForkliftController` custom resource:

```python
snapshots_interval = py_config["snapshots_interval"]
forklift_controller.wait_for_condition(
    status=forklift_controller.Condition.Status.TRUE,
    condition=forklift_controller.Condition.Type.RUNNING,
    timeout=300,
)

LOGGER.info(
    f"Updating forklift-controller ForkliftController CR with snapshots interval={snapshots_interval} seconds"
)

with ResourceEditor(
    patches={
        forklift_controller: {
            "spec": {
                "controller_precopy_interval": str(snapshots_interval),
            }
        }
    }
):
```

What these values do:

- `snapshots_interval` controls how often Forklift schedules warm precopy snapshots.
- `mins_before_cutover` controls how far in the future cutover is scheduled.
- `plan_wait_timeout` controls how long the suite waits for the migration to finish.

The defaults in this repository are a fast `2` second precopy interval and a `5` minute cutover delay.

> **Warning:** `snapshots_interval` is not treated as a per-plan setting in the test suite. Warm tests patch the live `forklift-controller` `ForkliftController` resource while the fixture is active.

## Cutover Timing

Warm tests do not cut over immediately by default. They compute the cutover timestamp in UTC from `mins_before_cutover`:

```python
def get_cutover_value(current_cutover: bool = False) -> datetime:
    datetime_utc = datetime.now(pytz.utc)
    if current_cutover:
        return datetime_utc

    return datetime_utc + timedelta(minutes=int(py_config["mins_before_cutover"]))
```

That helper is passed into the migration execution step:

```python
execute_migration(
    ocp_admin_client=ocp_admin_client,
    fixture_store=fixture_store,
    plan=self.plan_resource,
    target_namespace=target_namespace,
    cut_over=get_cutover_value(),
)
```

This gives the warm migration time to accumulate precopy work before the final switchover.

Use these tuning rules:

- Increase `mins_before_cutover` if you want a longer warm phase before downtime.
- Decrease `mins_before_cutover` if you want the test to reach final cutover sooner.
- Increase `plan_wait_timeout` if your environment is slow and the plan needs more time to finish.

> **Tip:** The helper also supports immediate cutover with `current_cutover=True`, although the warm test classes in this repository use the delayed default.

## Supported Providers

Warm provider support is enforced directly in the warm test modules.

| Source provider | Warm status in this repo | Notes |
| --- | --- | --- |
| vSphere | Supported | Main warm path. Also used for warm copy-offload coverage. |
| RHV | Implemented, but Jira-gated | The warm tests add a Jira marker for `MTV-2846`. |
| OpenStack | Not supported | Explicitly skipped by the warm tests. |
| OpenShift | Not supported as a warm source | Explicitly skipped by the warm tests. |
| OVA | Not supported | Explicitly skipped by the warm tests. |

The repository also includes a remote-destination warm scenario:

- `TestWarmRemoteOcp` runs a warm migration to a remote OpenShift provider.
- It requires `remote_ocp_cluster` to be configured.
- It uses the `remote` marker rather than the `warm` marker.

Marker definitions come from `pytest.ini`:

```ini
markers =
    tier0: Core functionality tests (smoke tests)
    remote: Remote cluster migration tests
    warm: Warm migration tests
    copyoffload: Copy-offload (XCOPY) tests
```

> **Note:** Most warm suites are selected with the `warm` marker. The remote OpenShift warm scenario lives in `tests/test_mtv_warm_migration.py`, but it is selected through `remote`.

## Warm Scenarios Included In The Repository

These are the main warm configurations currently defined in `tests/tests_config/config.py`:

- `test_sanity_warm_mtv_migration`: basic warm migration of a powered-on RHEL VM
- `test_mtv_migration_warm_2disks2nics`: warm migration with extra disk and NIC coverage
- `test_warm_remote_ocp`: warm migration to a remote OpenShift destination
- `test_warm_migration_comprehensive`: warm migration with static IP, PVC naming, target labels, affinity, and custom VM namespace coverage
- `test_copyoffload_warm_migration`: warm migration through the vSphere copy-offload path

## What Warm Tests Validate

Warm tests use the standard post-migration validation path in `check_vms()`. That means a warm run is not only checking whether MTV finishes, but also whether the migrated VM looks correct afterward.

The standard validation path covers:

- VM power state, including `target_power_state` when it is set
- CPU and memory
- network mapping
- storage mapping
- provider SSL configuration for VMware, RHV, and OpenStack providers
- guest agent availability when the plan expects it
- SSH connectivity when the destination VM is powered on
- VMware snapshot tracking when snapshot data exists
- VMware serial preservation on the destination VM

This is especially useful for warm migrations because the destination VM is usually powered on after cutover, which allows the suite to verify guest-agent and SSH behavior immediately.

> **Note:** SSH-based checks run only when the destination VM is powered on. If you set a powered-off target state, those checks are skipped.

## The Comprehensive Warm Validation Path

The most feature-rich warm scenario in the repository is `test_warm_migration_comprehensive`:

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

This configuration adds several warm-specific checks on top of the baseline `check_vms()` flow:

- `preserve_static_ips` triggers static IP validation after cutover
- `vm_target_namespace` moves the migrated VM into a custom namespace
- `pvc_name_template` enables PVC name verification against the rendered Forklift template
- `pvc_name_template_use_generate_name` switches PVC validation to prefix matching, because Kubernetes adds a random suffix
- `target_labels` verifies labels on the migrated VM
- `target_affinity` verifies the affinity block on the destination VM

A few details are easy to miss:

- In `target_labels`, a value of `None` means the suite replaces that value with the session UUID at runtime.
- When `vm_target_namespace` is set, the fixture creates that namespace if needed before migration.
- PVC template rendering supports Go template syntax and Sprig functions in the validation path.

> **Note:** Static IP preservation is currently validated only for Windows VMs migrated from vSphere.

> **Note:** The `{{.FileName}}` and `{{.DiskIndex}}` PVC template verification path is VMware-specific in the current code.

## Warm Copy-Offload: The Extra Validation Path

The warm copy-offload scenario adds one more validation step after the standard warm checks. In `tests/test_copyoffload_migration.py`, the suite first runs the normal post-migration verification and then explicitly verifies disk count:

```python
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
verify_vm_disk_count(
    destination_provider=destination_provider, plan=prepared_plan, target_namespace=target_namespace
)
```

That extra assertion matters for the copy-offload path because it confirms that the migrated VM still has the expected disk inventory after the accelerated transfer flow.

The corresponding warm test plan looks like this:

```python
"test_copyoffload_warm_migration": {
    "virtual_machines": [
        {
            "name": "xcopy-template-test",
            "source_vm_power": "on",
            "guest_agent": True,
            "clone": True,
            "disk_type": "thin",
        },
    ],
    "warm_migration": True,
    "copyoffload": True,
},
```

This path is stricter about provider requirements than the standard warm path:

- the source provider must be vSphere
- the provider configuration must include a `copyoffload` section
- the storage credentials and required copy-offload fields must be present

> **Warning:** Warm copy-offload is not a generic warm migration path. The fixture fails early unless the source provider is vSphere and the required `copyoffload` settings are configured.

## Provider Configuration For Full Warm Validation

If you want the full warm validation path, especially guest-agent and SSH-based checks, your provider entry needs guest VM credentials. The vSphere example in `.providers.json.example` includes them:

```jsonc
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
```

The warm copy-offload path expects a `copyoffload` subsection under the vSphere provider entry. The example file includes fields such as `storage_vendor_product`, `datastore_id`, `storage_hostname`, `storage_username`, and `storage_password`.

> **Note:** The example provider file uses comments for secret-scanning exceptions. Those comments are fine in the example file, but they are not valid in strict JSON.

## Practical Guidance

- Start with `test_sanity_warm_mtv_migration` when you want to prove your environment can complete a basic warm cycle.
- Move to `test_mtv_migration_warm_2disks2nics` when you want more disk and network coverage without turning on every advanced feature.
- Use `test_warm_migration_comprehensive` when you specifically need to validate static IP preservation, PVC naming, target labels, affinity, and custom VM namespace placement.
- Use `test_copyoffload_warm_migration` only when your environment is already prepared for vSphere copy-offload.
- Increase `mins_before_cutover` if your goal is to observe more precopy work before final downtime.
- Keep `target_power_state: "on"` when you want SSH-based post-migration validation to run.
