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
| `utilities/copyoffload_migration.py` | copy-offload orchestration and XCOPY verification | `execute_copyoffload_migration()`, `verify_xcopy_used()`, `verify_populator_throttling()`, `verify_dedicated_migration_host()` |
| `utilities/deep_inspection.py` | Deep Inspection `Conversion` CR lifecycle | `create_conversion_resource()`, `wait_for_conversion_complete()`, `verify_di_results()` |
| `utilities/shared_disk.py` | shared-disk verification on migrated VMs | `verify_shared_disk_data()`, `label_shared_disk_on_source_windows()`, `verify_shared_disk_data_windows()` |
| `utilities/aap.py` | AWX/AAP deployment for hook tests | `deploy_awx_via_helm()`, `create_awx_job_template()`, `teardown_awx()` |
| `utilities/hooks.py` | Forklift `Hook` CR creation and failure validation | `create_hook_if_configured()`, `validate_hook_failure_and_check_vms()` |
| `utilities/provider_inventory.py` | Forklift inventory refresh and clone waits | `wait_for_cloned_vms_in_forklift_inventory()`, `validate_source_vms_exist()` |
| `utilities/pytest_utils.py` | failure data collection, session teardown, JUnit enrichment | `collect_created_resources()`, `session_teardown()`, `enrich_junit_xml()` |
| `utilities/ssh_utils.py` | SSH into migrated VMs via `virtctl` port-forward plus `python-rrmngmnt` | `VMSSHConnection`, `SSHConnectionManager`, `create_vm_ssh_connection()` |
| `utilities/upgrade.py` | MTV operator upgrade run | `run_mtv_upgrade()` |
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
`background()`.

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
| `wait_for_migration_complate()` | poll a migration, with an optional per-poll status callback |
| `wait_for_dual_migration_completion()` | wait for two plans migrating concurrently |
| `wait_for_concurrent_migration_execution()` | start several migrations and wait for all of them |
| `get_plan_migration_status()` / `get_vm_suffix()` | status parsing helpers |
| `resolve_pvc_name_template()` | resolve the per-provider PVC name template |
| `verify_vm_disk_count()` | assert the migrated disk count matches the source |

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

`run_mtv_upgrade()` runs the MTV operator upgrade used by the `upgrade` suite, with process-group timeouts so a stuck upgrade cannot hang the run.

## Validation

### `utilities/post_migration.py`

Validation of migrated VMs (2083 lines). `check_vms()` is the entry point used by `test_check_vms`; everything else is a focused check it composes.

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

### `utilities/shared_disk.py`

`verify_shared_disk_data()` mounts, writes, and reads a shared disk from both migrated VMs (Linux, 6-step pattern). `label_shared_disk_on_source_windows()` labels the shared NTFS
volume on the source through the VMware Guest Operations API before migration, and `verify_shared_disk_data_windows()` verifies it afterwards (Windows, 7-step pattern).

### `utilities/ssh_utils.py`

SSH into migrated VMs through `virtctl` port-forward plus `python-rrmngmnt`, so it works when cluster nodes have no external IPs. `VMSSHConnection` wraps a connection,
`SSHConnectionManager` manages one connection per VM for fixtures, `create_vm_ssh_connection()` builds a single connection, and `run_cmd_in_vm()` runs a command in the guest.

### `utilities/virtctl.py`

`download_virtctl_from_cluster()` fetches the matching `virtctl` binary and `add_to_path()` puts it on `PATH` for the session.

### `utilities/vmware_guest_operations.py`

Commands executed inside vSphere guests via the Guest Operations API: `run_command_in_vmware_guest()`, `detect_vmware_ip_origins_via_guest_ops()`, `detect_guest_nic_names()`,
`create_data_integrity_marker()`.

### `utilities/worker_node_selection.py`

Target worker node selection for placement tests: `get_worker_nodes()`, `parse_prometheus_value()`, `parse_prometheus_memory_metrics()`, `select_node_by_available_memory()`.

### `utilities/esxi.py`

Direct-ESXi host access for the `endpoint_type: esxi` source path: `install_ssh_key_on_esxi()`, `remove_ssh_key_from_esxi()`, raising `ESXiError` on failure.

### `utilities/provider_inventory.py`

Inventory-side waits that keep plan data consistent with Forklift's view: `force_inventory_refresh()`, `wait_for_added_nics_in_forklift_inventory()`,
`wait_for_cloned_vms_in_forklift_inventory()`, `validate_source_vms_exist()`.

## Teardown and Diagnostics

### `utilities/pytest_utils.py`

`is_dry_run()` (`--collect-only` / `--setup-plan`), `prepare_base_path()`, `setup_ai_analysis()` (rootcoz-backed failure analysis; disabled without `ROOTCOZ_SERVER_URL`),
`collect_created_resources()` (must-gather on failure), `resolve_item_plan_config()` (a collected item's plan config, failing fast when a non-parametrized item has no
`tests_params` entry), `teardown_resources()`, `session_teardown()`, `enrich_junit_xml()`.

### `utilities/must_gather.py`

`run_must_gather()` runs the full diagnostic collection and `run_plan_must_gather()` the plan-targeted one; both report whether they succeeded and
share image resolution.
`collect_must_gather_for_item()`, `collect_class_must_gather()`, `collect_class_teardown_must_gather()`, `flush_pending_class_must_gathers()` and the
`mark_class_pending_must_gather()` / `class_must_gather_collected()` / `mark_class_must_gather_collected()` bookkeeping attempt at most one successful gather per failing
class-plan instance per worker. Operational failures stay pending for retry; artifacts are not guaranteed if retries fail. `class_plan_identity()` uses the Class collector node ID, class-scoped callspec indices and worker ID, shared across ordinary
methods but distinct across plan parameter sets. `collect_class_must_gather(item, nextitem)` runs before default fixture finalization at class-plan boundaries,
including early exits with `nextitem=None`, and binds the current Plan or explicit None for collection and all retries, even if no failure is pending yet.
`initialize_class_plan_context()` runs from a `tryfirst` protocol wrapper before setup, clearing stale Plan attributes on identity transitions but not across
consecutive methods of the same plan. `collect_must_gather_for_item()` takes this explicit Plan context instead of rereading mutable class state.
Earlier method teardown failures stay pending until a boundary; session finish retries exceptional remaining cases on surviving workers, without guaranteeing
pre-cleanup state or crash recovery. Unbound fallback contexts read the class attribute only for the active identity; others bind None for a full gather.

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

`vmware.py` also exposes `format_insufficient_capacity_message()` and `format_capacity_validation_log()`; module constant `VSPHERE_NIC_DEVICE_KEY_OFFSET` documents the vSphere
device-key space used when adding NICs.

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
| Migration | `MigrationNotFoundError`, `MigrationStatusError`, `MigrationPlanExecError` |
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
