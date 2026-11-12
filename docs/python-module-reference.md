# Python Module Reference

The suite is deliberately thin at the test-method level: a test class creates resources and asserts on results, while everything reusable lives in `utilities/`, `libs/`,
`exceptions/`, `cli/`, and `scripts/hooks/`. This page is the map of those modules and their public entry points.

Most test authors never import these modules directly. They consume them through fixtures in `conftest.py` — `source_provider`, `prepared_plan`, `source_provider_inventory`,
`target_namespace`, `virtctl_binary`, `fixture_store`, `cleanup_migrated_vms` — and through the plan/verification helpers in the test class itself.

> **Note:** These modules drive live OpenShift and MTV environments. Repository automation only validates collection and setup: `tox.toml` runs `uv run pytest --setup-plan` and
> `uv run pytest --collect-only`, and the container image defaults to `uv run pytest --collect-only`.

## At a Glance

| Module | What it handles | Main entry points |
| --- | --- | --- |
| `utilities/utils.py` | cluster client, provider loading, provider CRs, config coercion | `get_cluster_client()`, `load_source_providers()`, `create_source_provider()`, `get_value_from_py_config()` |
| `utilities/resources.py` | tracked creation of every OpenShift/MTV resource | `create_and_store_resource()`, `unregister_teardown_resource()`, `get_or_create_namespace()` |
| `utilities/mtv_migration.py` | storage maps, network maps, plans, migrations | `get_storage_migration_map()`, `get_network_migration_map()`, `create_plan_resource()`, `execute_migration()`, `wait_for_migration_complate()` |
| `utilities/post_migration.py` | end-to-end and focused VM validation | `check_vms()` plus the `check_*` / `verify_*` helpers |
| `utilities/guest_ip_mode.py` | per-network guest IP mode verification on migrated VMs: `dhcp` (Windows and Linux) and `none` (Linux only); NetworkMap and DNS-data checks for overrides. `preserve` is verified by `check_static_ip_preservation()` | `verify_network_ip_modes()`, `verify_network_map_network_ip_mode()`, `verify_preserve_mode_dns_data()` |
| `utilities/copyoffload_migration.py` | copy-offload orchestration and XCOPY verification | `execute_copyoffload_migration()`, `verify_xcopy_used()`, `verify_populator_throttling()`, `verify_dedicated_migration_host()` |
| `utilities/deep_inspection.py` | Deep Inspection `Conversion` CR lifecycle | `create_conversion_resource()`, `wait_for_conversion_complete()`, `verify_di_results()` |
| `utilities/shared_disk.py` | shared-disk verification on migrated VMs | `verify_shared_disk_data()`, `label_shared_disk_on_source_windows()`, `verify_shared_disk_data_windows()` |
| `utilities/aap.py` | AWX/AAP deployment for hook tests | `deploy_awx_via_helm()`, `create_awx_job_template()`, `teardown_awx()` |
| `utilities/hooks.py` | Forklift `Hook` CR creation and failure validation | `create_hook_if_configured()`, `validate_hook_failure_and_check_vms()` |
| `utilities/provider_inventory.py` | Forklift inventory refresh and clone waits | `wait_for_cloned_vms_in_forklift_inventory()`, `validate_source_vms_exist()` |
| `utilities/pytest_utils.py` | failure data collection, session teardown, JUnit enrichment | `collect_created_resources()`, `session_teardown()`, `enrich_junit_xml()` |
| `utilities/data_collector.py` | the run's artifact directory, artifact-name timestamps and secret redaction, shared by every artifact writer | `configure_data_collector_path()`, `data_collector_path()`, `utc_timestamp()`, `redact_secret()` |
| `utilities/cluster_state.py` | bounded, never-raising cluster snapshots for failures and the session-start baseline | `dump_cluster_state()`, `write_session_baseline()` |
| `utilities/ssh_utils.py` | SSH into migrated VMs via `virtctl` port-forward plus `python-rrmngmnt` | `VMSSHConnection`, `SSHConnectionManager`, `create_vm_ssh_connection()` |
| `utilities/upgrade.py` | MTV operator upgrade run | `run_mtv_upgrade()`, `resolve_upgrade_timeout()` |
| `libs/providers/` | one adapter per source provider type | `VMWareProvider`, `OvirtProvider`, `OpenStackProvider`, `OCPProvider`, `OVAProvider`, `HyperVProvider` |
| `exceptions/exceptions.py` | every custom exception the suite raises | 24 exception classes, all in one module |

## Core Setup

### `utilities/utils.py`

The module that turns configuration into live connections. `load_source_providers()` reads `.providers.json`, `get_cluster_client()` builds the OpenShift `DynamicClient`, and
`get_value_from_py_config()` coerces string booleans such as `"true"` into real booleans so the rest of the suite treats settings consistently.

It also owns provider resource creation: `create_source_provider()` handles the per-provider differences (Secret keys, CA certificate fetching, copy-offload settings) for every
supported source type, and `create_source_cnv_vms()` builds the target VMs from plan data.

Other public helpers: `resolve_providers_json_path()`, `generate_class_hash_prefix()`, `gen_network_map_list()`, `get_per_nic_networks()`, `populate_vm_ids()`,
`extract_vm_from_plan()`, `get_cluster_version()`, `get_cluster_version_str()`, `get_mtv_version()`, `has_mtv_minimum_version()`, `delete_all_vms()`, `generate_ca_cert_file()`,
`background()`, `restore_skip_clone_power_states()`.

`restore_skip_clone_power_states()` puts shared `skip_clone` source VMs back to the power state recorded before plan setup changed it. It drains
its list, so the setup-failure finalizer and the class teardown call never restore twice, and it attempts every VM before re-raising the first
provider failure.

Provider shortcuts used by fixtures: `vmware_provider()`, `rhv_provider()`, `openstack_provider()`, `ova_provider()`, `ocp_provider()`, `hyperv_provider()`.

Constants: `DEFAULT_PROVIDERS_JSON_PATH`.

The module also defines `VirtualMachineFromInstanceType`, a `VirtualMachine` subclass that builds a full VM spec from instancetype/preference plus a few parameters.

### `utilities/resources.py`

Resource lifecycle foundation. `create_and_store_resource()` fills in the client, derives a name from `name`/`kind_dict`/`yaml_file` or generates one from the session base name,
appends `-warm`/`-cold` to `Plan` and `Migration` names, truncates to 63 characters, deploys and waits, and records the resource in `fixture_store["teardown"]` so teardown,
`resources.json`, and leftover detection know about it.

`get_or_create_namespace()` builds on it and applies the suite's standard namespace labels. `unregister_teardown_resource()` removes a resource you deleted yourself from teardown
tracking — required when a test archives and deletes a Plan mid-run.

> **Tip:** Use `create_and_store_resource()` for anything that creates a cluster object during a test. That is what makes later teardown and leftover detection work.

### `utilities/logger.py`

`setup_logging()` configures the suite logger; `separator()` prints a visual break between long-running phases.

### `utilities/naming.py`

Name generation and sanitisation: `generate_name_with_uuid()`, `sanitize_kubernetes_name()`, `resolve_destination_vm_name()`, `sanitize_test_name_for_path()`.
`resolve_destination_vm_name()` matters during teardown because MTV can rename a migrated VM.

## Migration Orchestration

### `utilities/mtv_migration.py`

The module most test authors reuse first. It owns `StorageMap`, `NetworkMap`, `Plan`, and `Migration` creation plus the waits for their states.

| Function | Purpose |
| --- | --- |
| `get_storage_migration_map()` | build and deploy the `StorageMap` |
| `get_network_migration_map()` | build and deploy the `NetworkMap` |
| `create_plan_resource()` | build and deploy the `Plan`, including plan flags |
| `execute_migration()` | create the `Migration` and poll it to completion |
| `get_migration_for_plan()` | locate the `Migration` CR belonging to a `Plan` |
| `wait_for_migration_complate()` | poll a migration, with an optional per-poll status callback and an unschedulable-VM fail-fast |
| `wait_for_dual_migration_completion()` | wait for two plans migrating concurrently |
| `wait_for_concurrent_migration_execution()` | start several migrations and wait for all of them |
| `get_plan_migration_status()` / `get_vm_suffix()` | status parsing helpers |
| `resolve_pvc_name_template()` | resolve the per-provider PVC name template |
| `verify_vm_disk_count()` | assert the migrated disk count matches the source |

`wait_for_migration_complate()` polls `Plan` status every second until `Succeeded` or `Failed`, and additionally re-reads the destination
`VirtualMachineInstance` objects of the plan's VMs every 30 seconds. When the scheduler reports `PodScheduled=False` with reason
`Unschedulable` on the same VM for 3 consecutive checks, the wait raises `MigrationVmUnschedulableError` naming the VM and the raw
scheduler message. The probe matches the destination names the plan reports plus each spec VM's resolved destination name (`targetName`, or the
sanitised source name), keeps a consecutive count per VM, and runs only on non-terminal polls — a plan that reports `Failed` still raises
`MigrationPlanExecError` carrying its status. On the poll that reports `Failed`, `wait_for_migration_complate()` first captures the failure
evidence (see [Failure Evidence Dumps](troubleshooting-and-diagnostics.html#failure-evidence-dumps)): MTV deletes the failed objects before must-gather runs, so the
plan's `Conversion` CRs are also re-read every 10 seconds while the plan waits and the last non-empty dump is written when the CR is already gone. The suite never frees
destination node capacity, so a scheduler rejection is terminal: aborting after ~60-90 seconds
is preferable to running to `plan_wait_timeout` (1 hour) for a VM that can never start.

### `utilities/migration_utils.py`

Cancel, archive, and cleanup flows: `cancel_migration()`, `archive_plan()`, `get_orphan_resource_names()`, `check_dv_pvc_pv_deleted()`, `append_leftovers()`, `get_cutover_value()`.

### `utilities/copyoffload_migration.py`

The largest feature module (2470 lines). It owns credential resolution, cloud-init readiness, XCOPY verification, concurrency tracking, and dedicated-host behaviour.

| Area | Functions |
| --- | --- |
| Credentials | `get_copyoffload_credential()`, `parse_storage_secret_extra_env()`, `get_storage_secret_extra()`, `merge_storage_secret_extra()` (env overrides config; extras override vendor keys) |
| Execution | `execute_copyoffload_migration()`, `execute_migration_monitoring_populator_inflight()`, `execute_migration_monitoring_vm_and_populator_inflight()`, `apply_copyoffload_vm_name_override()`, `get_migration_uid()` |
| Log capture | `capture_populate_pod_logs()`, `create_log_capture_callback()` |
| Cloud-init | `wait_for_vmware_cloud_init_all_vms()`, `wait_for_cloud_init()` |
| Verification | `verify_xcopy_used()`, `verify_xcopy_used_per_datastore()`, `verify_populator_throttling()`, `verify_populator_inflight_observed()`, `verify_vm_inflight_throttling()`, `verify_dedicated_migration_host()`, `resolve_invalid_dedicated_host_id()`, `verify_populate_pod_failure_reason()` |
| Host selection | `get_configured_dedicated_hosts()`, `resolve_non_dedicated_esxi_host()` |

Internal trackers `_PopulatorConcurrencyTracker` and `_VmConcurrencyTracker` record peak per-host concurrency while the migration is polled.

Constants include `STORAGE_SECRET_EXTRA_ENV` (the environment variable that injects extra Secret keys) and `PVC_NAME_LABEL`.

### `utilities/copyoffload_plan_secret.py`

`plan_uses_copyoffload()`, `wait_for_plan_secret()`, `wait_for_copyoffload_plan_secret()` — poll for the populator secret Forklift creates when a copy-offload migration starts.
Constants: `PLAN_SECRET_WAIT_TIMEOUT`, `PLAN_NAME_LABEL`, `POPULATOR_LABEL`, `COPY_OFFLOAD_PVC_NAME_TEMPLATE`.

### `utilities/copyoffload_constants.py`

`SUPPORTED_VENDORS`, `POPULATOR_INFLIGHT_LIMIT`, `VM_INFLIGHT_LIMIT`, `VM_POPULATOR_INFLIGHT_LIMIT`, `SOURCE_HOST_LABEL`, `POPULATOR_THROTTLED_EVENT_REASON`,
`FORKLIFT_CONTROLLER_NAME`.

### `utilities/copyoffload_datastore.py`

`resolve_datastore_moid_from_disk_config()` and `format_custom_datastore_not_found_message()` resolve a disk's datastore, with explicit errors for the symbolic secondary and
non-XCOPY datastore placeholders (`ERR_SECONDARY_DS_NOT_CONFIGURED`, `ERR_NON_XCOPY_DS_NOT_CONFIGURED`, `ERR_EMPTY_DISK_DATASTORE_ID`).

### `utilities/forklift_controller_populator.py`

Reads and sets the in-flight limits on the `forklift-volume-populator-controller` Deployment: `get_populator_inflight_from_deployment()`, `populator_inflight_limit()`,
`wait_for_populator_inflight_deployment()`, `get_vm_inflight_from_deployment()`, `vm_inflight_limit()`, `wait_for_vm_inflight_deployment()`, plus the cross-worker file-lock
helpers `ensure_secure_shared_lock_dir()`, `get_forkliftcontroller_populator_inflight_lock_path()`, `get_forkliftcontroller_vm_populator_inflight_lock_path()`.

### `utilities/hooks.py`

`validate_hook_config()`, `validate_custom_playbook()`, `create_hook_for_plan()`, `validate_all_vms_same_step()`, `validate_expected_hook_failure()`,
`validate_hook_failure_and_check_vms()`, `create_hook_if_configured()`.

### `utilities/deep_inspection.py`

Deep Inspection (`Conversion` CR) lifecycle for both standalone and plan-driven DI: `create_di_connection_secret()`, `create_conversion_resource()`, `wait_for_conversion_phase()`,
`wait_for_conversion_complete()`, `cancel_conversion()`, `wait_for_conversion_pods()`, `wait_for_conversion_pods_cleanup()`, `wait_for_di_snapshot()`,
`wait_for_di_snapshot_cleanup()`, `verify_di_results()`, `verify_captured_di_results()`, `verify_di_concerns_block_migration()`, `get_plan_conversion_crs()`,
`wait_for_critical_conditions()`, `create_di_capture_callback()`.

### `utilities/upgrade.py`

`run_mtv_upgrade()` runs the MTV operator upgrade used by the `upgrade` suite, with process-group timeouts so a stuck upgrade cannot hang the run. Its outer wait budget comes
from `resolve_upgrade_timeout()`: the `--mtv-upgrade-timeout` option, then `MTV_UPGRADE_TIMEOUT_SECONDS`, then 5400 seconds, so the budget stays above the upgrade script's own
internal waits and the script reports its own timeout instead of being killed by the harness. The script writes into a temporary directory that is deleted before the function
returns; only the redacted text is persisted under `<data-collector>/upgrade-script-<timestamp>/`, so `CLUSTER_PASSWORD` never reaches the artifact tree, and `MtvUpgradeError`
carries the tail of both redacted logs plus those paths. A missing or empty `cluster_password` raises before the script starts. A `scope="upgrade"` cluster-state snapshot is
written before both the outer-timeout raise and the non-zero-exit raise, reading the cluster through the `client` the caller passes; without a client the snapshot is skipped rather than collected some other way.
Under `--skip-data-collector` nothing is persisted: the child still writes its stdout and stderr into a
temporary directory that is deleted before the function returns, and the error carries the redacted tails of both streams.

### `utilities/cluster_state.py`

`dump_cluster_state(reason=..., scope="upgrade"|"storage", namespace=..., client=..., data_collector_path=...)` writes one timestamped, bounded snapshot per failure and never
raises, so the failing exception stays the error the caller sees. `client` is required: every probe reads the cluster through an `ocp_resources` wrapper (a small summary-list
helper reads a kind's listing in one request), so collection uses no `oc` binary, no `subprocess` and no kubeconfig — a token never reaches the disk. There is no `ocp_resources`
wrapper for the cluster-wide image config in this release, so that one probe reads `config.openshift.io/v1` `Image` through the client's generic kind API. Each probe runs under
its own `try`/`except` over the exceptions the client can raise, including the wrapper's missing-API `NotImplementedError`, and renders as `[probe failed: …]` instead of escaping.
`data_collector_path` overrides the configured root when given; the collector root is resolved first, so `--skip-data-collector` returns before the first probe, and every artifact
is written with the resolved `CLUSTER_PASSWORD` redacted. Each snapshot also has a total budget (`_SNAPSHOT_DEADLINE_SECONDS`, 120 seconds, `monotonic()`): probes stop once it
expires and the file closes with a "snapshot deadline reached … N probes skipped" record, so a hanging API server cannot delay the original error by minutes. Each probe's output is
capped at `_MAX_PROBE_OUTPUT_BYTES` (512 KiB) with an explicit `[truncated: <N> bytes omitted]` marker. `write_session_baseline()` writes `<data-collector>/session-start/cluster.txt`
and `run-identity.txt` once per session from `pytest_sessionstart`, tolerating a missing `.git` (the revision is read from git's files, no subprocess) and skipping cleanly under
`--skip-data-collector`; it builds a client for the cluster probes when none is passed and skips the cluster section if that fails, so the hook never raises. Only the xdist
controller (or the sole process without xdist) wipes the data-collector root and writes the baseline, and the files are created exclusively, so a concurrent writer never replaces
an existing snapshot.

### `utilities/data_collector.py`

Holds the resolved artifact directory as module state, because most writers have no pytest `config` to read it from: `configure_data_collector_path()` is called once in
`pytest_sessionstart` (`None` under `--skip-data-collector`), `data_collector_path()` returns it, `utc_timestamp()` names timestamped artifacts, and `redact_secret()` keeps the
cluster password out of every artifact.

## Validation

### `utilities/post_migration.py`

Validation of migrated VMs (2083 lines). `check_vms()` is the entry point used by `test_check_vms`; everything else is a focused check it composes. `verify_excluded_disks()` orchestrates the disk-exclusion verification (per-VM kept-disk loop plus the namespace PVC-count assertion) and delegates per-VM disk-identity verification to the vSphere provider module `libs.providers.vmware.verify_kept_disks_attached`, since exclude_disks uses vSphere bus addresses.

| Area | Functions |
| --- | --- |
| Connectivity | `get_ssh_credentials_from_provider_config()`, `check_ssh_connectivity()`, `check_vm_command_output()` |
| Compute | `check_cpu()`, `check_cpu_features()`, `check_memory()`, `check_vbs_status()` |
| Boot/BIOS | `check_boot_configuration()`, `check_serial_preservation()` |
| State | `check_vms_power_state()`, `check_false_vm_power_off()`, `check_guest_agent()`, `check_snapshots()`, `check_disconnected_nic_state()` |
| Network | `check_network()`, `get_nic_by_mac()`, `get_all_destinations()`, `check_nic_name_preservation()`, `check_static_ip_preservation()` |
| Storage | `check_storage()`, `check_pvc_names()`, `verify_data_integrity()`, `verify_luks_encryption()`, `verify_rdm_disk_bus_types()` |
| Placement | `check_vm_node_placement()`, `check_vm_labels()`, `check_vm_affinity()`, `check_ssl_configuration()` |

Static-IP preservation applies to vSphere and Hyper-V sources and supports both Windows (`ipconfig /all`) and Linux (`nmcli device show`) guests.

`check_ssh_connectivity()` waits for the guest to serve SSH sessions through the shared `_wait_for_ssh_connection()`
retry (300s), because a freshly migrated guest accepts the port-forward tunnel before its OpenSSH server is ready.
`check_vbs_status()` retries the DeviceGuard query itself in the same window, since a guest that answered a probe can
still refuse the next session.

### `utilities/shared_disk.py`

`verify_shared_disk_data()` mounts, writes, and reads a shared disk from both migrated VMs (Linux, 6-step pattern). `label_shared_disk_on_source_windows()` labels the shared NTFS
volume on the source through the VMware Guest Operations API before migration, and `verify_shared_disk_data_windows()` verifies it afterwards (Windows, 7-step pattern).

### `utilities/ssh_utils.py`

SSH into migrated VMs through `virtctl` port-forward plus `python-rrmngmnt`, so it works when cluster nodes have no external IPs. `VMSSHConnection` wraps a connection,
`SSHConnectionManager` manages one connection per VM for fixtures, `create_vm_ssh_connection()` builds a single connection, and `run_cmd_in_vm()` runs a command in the guest.
`VMSSHConnection.connect()` raises `ConnectionError` for a paramiko `SSHException` (a guest that
is not yet serving sessions), `RuntimeError` when the port-forward cannot be established and when the `echo test` probe fails, and re-raises
`OSError` unchanged.

### `utilities/virtctl.py`

`download_virtctl_from_cluster()` fetches the matching `virtctl` binary — a `virtctl` already on `PATH` is never reused, only a previously downloaded binary in the cluster-versioned cache — and `add_to_path()` puts the downloaded directory on `PATH` for the session.

### `utilities/vmware_guest_operations.py`

Commands executed inside vSphere guests via the Guest Operations API: `run_command_in_vmware_guest()`, `detect_vmware_ip_origins_via_guest_ops()`, `detect_guest_nic_names()`,
`create_data_integrity_marker()`, and `find_alternate_linux_os_root()`. The latter requires a Linux guest and root Guest Operations credentials, reads the active OS identity and `lsblk` device inventory in separate Guest Operations calls, and inspects supported ext/XFS single-letter `/dev/sd` partitions read-only. Partition inspection and `umount` use guest-side timeouts; an EXIT trap also attempts to remove the temporary mount directory. It uses `lsblk --pairs`, available in util-linux 2.23.2; JSON output was added in util-linux 2.27. The helper returns a `LinuxOSRoot` with the unique alternate device path, OS ID, and version. The current Forklift vSphere mapper does not translate NVMe or LVM paths to the corresponding disk index.

`run_command_in_vmware_guest()` terminates a Guest Operations process that exceeds its timeout. Callers may provide a timeout cleanup command for guest resources that need explicit cleanup after process termination.

### `utilities/worker_node_selection.py`

Target worker node selection for placement tests: `get_worker_nodes()`, `parse_prometheus_value()`, `parse_prometheus_memory_metrics()`, `select_node_by_available_memory()`.
`has_unschedulable_condition()` is shared with the migration unschedulable probe and validates its input: a list of condition mappings or `None`, else `ValueError`.

### `utilities/esxi.py`

Direct-ESXi host access for the `endpoint_type: esxi` source path: `install_ssh_key_on_esxi()`, `remove_ssh_key_from_esxi()`, raising `ESXiError` on failure.

### `utilities/provider_inventory.py`

Inventory-side waits that keep plan data consistent with Forklift's view: `force_inventory_refresh()`, `wait_for_added_nics_in_forklift_inventory()`,
`wait_for_cloned_vms_in_forklift_inventory()`, `validate_source_vms_exist()`.

## Teardown and Diagnostics

### `utilities/pytest_utils.py`

`is_dry_run()` (`--collect-only` / `--setup-plan`), `prepare_base_path()`, `setup_ai_analysis()` (rootcoz-backed failure analysis; disabled without `ROOTCOZ_SERVER_URL`),
`collect_created_resources()` (writes the tracked resource inventory), `resolve_item_plan_config()` (a collected item's plan config, failing fast when a non-parametrized item has no
`tests_params` entry), `teardown_resources()`, `session_teardown()`, `enrich_junit_xml()`.

### `utilities/must_gather.py`

`run_must_gather(data_collector_path, since_time)` runs the full diagnostic collection (`/usr/bin/gather`) with
`MUST_GATHER_SINCE_TIME` bounding the collected logs to the given UTC RFC 3339 timestamp, and reports whether it
succeeded. Acceptance is based on the diagnostics in the destination rather than the exit code — the OpenShift client's
disk-usage guard aborts gathers on nearly-full nodes while still exiting 0, so its limit is raised to 90 via
`--volume-percentage` and, while the destination holds none, attempts are retried pinned to Ready schedulable
worker nodes. Missing resources and the wrapper's specific resource API discovery failure return `False`; missing or invalid image metadata and other configuration/programming errors propagate.
Standalone setup/call collector errors stay in the failing item's typed stash until the teardown wrapper yields to fixture finalizers.
The pending-gather flush, resource inventory writing and configured cleanup run before the inner session-finish hooks yield,
so xdist worker shutdown cannot interrupt them; the summary and enabled AI enrichment run after those hooks, and errors are
re-raised at the end with earlier errors chained.
Independent `finally` blocks also attempt temporary-directory removal, the summary and enabled AI enrichment despite inventory or teardown errors. Cleanup errors propagate with earlier errors chained. Both teardown and session-finish use modern `wrapper=True` hooks so ordinary fixture exceptions do not emit old-style pluggy teardown warnings. Inventory write failures are logged by the existing inventory helper.
`collect_must_gather_for_item()`, `collect_class_must_gather()`, `collect_class_teardown_must_gather()`, `flush_pending_class_must_gathers()` and the
`mark_class_pending_must_gather()` / `class_must_gather_collected()` / `mark_class_must_gather_collected()` documented state API attempt at most one successful gather per failing
class-plan instance per worker. Operational failures stay pending for retry; artifacts are not guaranteed if retries fail. `class_plan_identity()` uses the Class collector node ID, class-scoped callspec indices and worker ID, shared across ordinary
methods but distinct across plan parameter sets. `collect_class_must_gather(item, nextitem)` runs before default fixture finalization at class-plan boundaries,
including early exits with `nextitem=None`.
`record_class_plan_start_time()` runs from a `tryfirst` protocol wrapper before setup, recording one start timestamp per class-plan identity,
first seen winning across xdist batch re-entry; `record_session_start_time()` / `session_start_time()` do the same for the session, scoping standalone and teardown gathers.
`collect_must_gather_for_item()` takes this explicit start timestamp instead of rereading mutable class state.
Earlier method teardown failures stay pending until a boundary; session finish retries exceptional remaining cases on surviving workers using each identity's recorded start time, without guaranteeing
pre-cleanup state or crash recovery.

### `utilities/aap.py`

AWX/AAP deployment and REST API helpers for hook integration tests: `is_awx_installed()`, `deploy_awx_via_helm()`, `create_awx_instance()`, `wait_for_awx_ready()`,
`wait_for_awx_api_ready()`, `get_awx_admin_password()`, `get_awx_route_url()`, `create_awx_auth_token()`, `create_awx_project()`, `wait_for_awx_project_sync()`,
`create_awx_inventory()`, `create_awx_job_template()`, `create_aap_token_secret()`, `teardown_awx()`.

## Provider Abstraction (`libs/`)

### `libs/base_provider.py`

`BaseProvider` is the abstract base every source adapter implements: `connect()` is context-manager managed — `__enter__()` calls it and `__exit__()` is a no-op that does **not** call
`disconnect()`, so an adapter that opens a session must close it explicitly — and the abstract surface
is `connect()`, `disconnect()`, `test`, `vm_dict`, `clone_vm()`, `delete_vm()`, `get_vm_or_template_networks()`. Shared behaviour includes `_generate_clone_vm_name()` and
`supports_skip_clone()`.

### `libs/providers/`

| Module | Class | Backing technology |
| --- | --- | --- |
| `vmware.py` | `VMWareProvider` | `pyvmomi` / vSphere Automation SDK |
| `rhv.py` | `OvirtProvider` | `ovirt-python-sdk` |
| `openstack.py` | `OpenStackProvider` | `openstacksdk` |
| `openshift.py` | `OCPProvider` | `openshift-python-wrapper` against a source cluster |
| `ova.py` | `OVAProvider` | OVA import over NFS |
| `hyperv.py` | `HyperVProvider` | PowerShell Remoting (PSRP) |

`vmware.py` also exposes `format_insufficient_capacity_message()`, `format_capacity_validation_log()`, and `verify_kept_disks_attached()` (vSphere-source per-VM kept-disk verification for disk exclusion: bus-address partitioning, source disk UUID serial prefixes, volume-mode-aware capacity checks); module constant `VSPHERE_NIC_DEVICE_KEY_OFFSET` documents the vSphere
device-key space used when adding NICs.

`openshift.py` starts destination VMs through `OCPProvider.vm_dict()`, which polls `printableStatus` via
`_wait_for_printable_status()` instead of sampling once: up to 120s for a stable `Running`/`Stopped` state when recording
`power_state`, and up to 300s for `Running` after `start_vm()`. `Starting` (`Ready: GuestNotRunning`) is a normal pre-boot
state and does not fail the test immediately — but a VM that is still `Starting` when the 300s wait expires does raise
`ValueError`, which names the last observed status and conditions. Only a confirmed `Stopped` reading is recorded as
`power_state="off"` (it drives the restore-to-off path); an unresolved pre-start status stays unset until the start wait
confirms `Running`. The module constants `_VM_STABLE_STATE_TIMEOUT_SECONDS`, `_VM_RUNNING_TIMEOUT_SECONDS`, and
`_VM_STATUS_POLL_SECONDS` own those numbers.

### `libs/forklift_inventory.py`

`create_forklift_inventory(client, mtv_namespace, provider)` returns the inventory implementation matching the provider type: `VsphereForkliftInventory`, `OvirtForkliftInventory`,
`OpenstackForliftinventory`, `OpenshiftForkliftInventory`, `OvaForkliftInventory`, `HypervForkliftInventory` — all subclasses of `ForkliftInventory`.

`ForkliftInventory` exposes `get_data()`, `vms`, `get_vm()`, `wait_for_vm()`, `vms_names`, `networks`, `storages`, `vms_storages_mappings`, `vms_networks_mappings`, and
provider-specific sync checks (OpenStack volumes and networks).

## Exceptions (`exceptions/exceptions.py`)

Every custom exception lives in this one module, enforced by the `check-exceptions-location` pre-commit hook.

| Area | Exceptions |
| --- | --- |
| Provider and cluster | `MissingProvidersFileError`, `ProviderEmptyContentError`, `MtvOperatorNotInstalledError`, `ForkliftPodsNotRunningError`, `RemoteClusterAndLocalCluterNamesError` |
| VM lifecycle | `VmNotFoundError`, `VmCloneError`, `VmMissingVmxError`, `VmBadDatastoreError`, `VmPipelineError`, `VmMigrationStepMismatchError`, `InvalidVMNameError` |
| Migration | `MigrationNotFoundError`, `MigrationStatusError`, `MigrationPlanExecError`, `MigrationVmUnschedulableError` |
| Guest access | `GuestCommandError`, `SSHConnectionSetupError`, `PowerShellCommandError`, `ConversionError` |
| Suite lifecycle | `SessionTeardownError`, `ResourceNameNotStartedWithSessionUUIDError`, `OvirtMTVDatacenterNotFoundError`, `OvirtMTVDatacenterStatusError`, `MtvUpgradeError` |

## CLI (`cli/mtv_api_tests/`)

A Typer app installed as the `mtv-api-tests` console script.

| Module | Role |
| --- | --- |
| `__init__.py` | `main()`, `generate()`, `run()`, and the `RunMode` enum (`local` / `job`) |
| `generate.py` | interactive wizard that writes `.providers.json` and the Job YAML |
| `run.py` | run tests locally (`uv run pytest`) or as an OpenShift Job |
| `common.py` | shared prompts and discovery: provider/vendors/VMs/ESXi hosts/storage classes, cluster connection and MTV validation, `build_providers_json()`, `write_providers_json()`, `mask_passwords()`, `generate_job_yaml()` |

`run` accepts `--mode`, `--category`, `--source-provider`, `--destination-provider`, `--storage-class`, `-k/--test-filter`, and `--job-yaml`.

## Repository Tooling

### `scripts/hooks/`

Eight local pre-commit hooks enforce the rules in `AGENTS.md` as AST checks, with a baseline ratchet (`baseline.py` + `scripts/hooks/baselines/*.txt`) so legacy violations do not
block unrelated work:

| Hook | Rule |
| --- | --- |
| `check_no_kubernetes_runtime.py` | no runtime `kubernetes` imports (except `kubernetes.dynamic.exceptions`) |
| `check_no_dynamicclient_construct.py` | no direct `DynamicClient(...)` construction |
| `check_no_except_exception.py` | no `except Exception` outside pytest hooks |
| `check_no_runtimeerror.py` | no `raise RuntimeError` outside pytest hooks |
| `check_exceptions_location.py` | exception subclasses belong in `exceptions/exceptions.py` |
| `check_test_file_location.py` | no `test_*.py` directly under `tests/` |
| `check_autouse_fixtures.py` | only `autouse_fixtures` may use `autouse=True` |
| `check_no_module_load_source_providers.py` | no module-level `load_source_providers()` in `tests/` |

### `tools/`

`tools/clean_cluster.py` (`clean_cluster_by_resources_file()`) removes leftovers listed in a `resources.json`; `tools/bm-dns-setup.sh` and `tools/update-branches.sh` are operator
helpers.
