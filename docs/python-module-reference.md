# Python Module Reference

The modules in `utilities/` are the shared plumbing behind the MTV API test suite. They create and track MTV/OpenShift resources, prepare providers, manage `virtctl`, open SSH sessions to migrated VMs, collect diagnostics, and run the post-migration checks that most tests depend on.

Most users do not call every helper directly. In practice, you reach them through fixtures in `conftest.py`, especially `target_namespace`, `source_provider`, `prepared_plan`, `virtctl_binary`, `vm_ssh_connections`, and `cleanup_migrated_vms`.

> **Note:** These modules are designed for live OpenShift and MTV environments. Repository automation only validates collection and setup: `tox.toml` runs `uv run pytest --setup-plan` and `uv run pytest --collect-only`, and the container image defaults to `uv run pytest --collect-only`.

## At a Glance

| Module | What it handles | Main entry points |
| --- | --- | --- |
| `utilities.utils` | cluster client setup, provider loading, provider CR creation | `get_cluster_client()`, `load_source_providers()`, `create_source_provider()` |
| `utilities.resources` | tracked creation of OpenShift and MTV resources | `create_and_store_resource()`, `get_or_create_namespace()` |
| `utilities.mtv_migration` | storage maps, network maps, plans, migrations | `get_storage_migration_map()`, `get_network_migration_map()`, `create_plan_resource()`, `execute_migration()` |
| `utilities.virtctl` | getting the right `virtctl` binary onto the test host | `download_virtctl_from_cluster()`, `add_to_path()` |
| `utilities.ssh_utils` | SSH access to migrated VMs over `virtctl port-forward` | `VMSSHConnection`, `SSHConnectionManager` |
| `utilities.post_migration` | end-to-end validation of migrated VMs | `check_vms()` and the focused `check_*` helpers |
| `utilities.hooks` | hook creation and hook-failure validation | `create_hook_if_configured()`, `validate_hook_failure_and_check_vms()` |
| `utilities.must_gather` | targeted MTV must-gather collection | `run_must_gather()` |
| `utilities.pytest_utils` | failure-time data collection and session cleanup | `collect_created_resources()`, `session_teardown()` |
| `utilities.migration_utils` | cancel/archive flows and storage cleanup | `cancel_migration()`, `archive_plan()`, `check_dv_pvc_pv_deleted()` |

## Core Setup

### `utilities.utils`

`utilities.utils` is where the suite turns configuration into live connections. `load_source_providers()` reads `.providers.json`, `get_cluster_client()` builds the OpenShift `DynamicClient`, and `get_value_from_py_config()` converts string booleans such as `"true"` and `"false"` into real Python booleans so the rest of the code can treat settings consistently.

This is also the module that creates source-side provider resources. `create_source_provider()` handles the provider-specific differences for VMware, RHV, OpenStack, OVA, and OpenShift, including creating the right `Secret` and `Provider` CRs, fetching CA certificates when SSL verification is enabled, and passing copy-offload settings through when present.

If you are writing tests rather than extending framework code, you usually consume this module indirectly through the `ocp_admin_client`, `source_provider_data`, and `source_provider` fixtures instead of importing it directly.

### `utilities.resources`

`utilities.resources` is the resource lifecycle foundation of the repository. Its core helper, `create_and_store_resource()`, does more than create a resource:

- It fills in the client automatically.
- It chooses a name from `name`, `kind_dict`, `yaml_file`, or generates one from the session base name.
- It appends `-warm` or `-cold` to `Plan` and `Migration` names.
- It truncates names to Kubernetes-safe length.
- It deploys and waits.
- It records the resource in `fixture_store["teardown"]` so later cleanup and diagnostics know it exists.

```19:68:utilities/resources.py
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

`get_or_create_namespace()` builds on that helper. It reuses an existing namespace when possible, but when it creates one itself it applies the standard labels used across this suite, including `pod-security.kubernetes.io/enforce=restricted`.

> **Tip:** Use `create_and_store_resource()` for anything that creates a cluster object during a test. That is what makes later teardown, `resources.json`, and leftover detection work.

## Migration Orchestration

### `utilities.mtv_migration`

`utilities.mtv_migration` is the module most test authors reuse first. It owns the standard flow for creating `StorageMap`, `NetworkMap`, `Plan`, and `Migration` resources and waiting for them to reach the right state.

Most tests follow the same pattern:

```96:147:tests/test_mtv_cold_migration.py
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

The most important entry points are:

- `get_storage_migration_map()`: Creates a `StorageMap`. In the normal case it derives mappings from provider inventory and uses `py_config["storage_class"]` unless you override it.
- `get_network_migration_map()`: Creates a `NetworkMap`. The first source network maps to the pod network, and additional networks map to generated Multus NADs.
- `create_plan_resource()`: Creates the `Plan` CR and waits for `Plan.Condition.READY=True`.
- `execute_migration()`: Creates the `Migration` CR and waits for the plan to finish.
- `wait_for_migration_complate()`: Polls the plan until it reaches `Succeeded` or `Failed`.
- `verify_vm_disk_count()` and `wait_for_concurrent_migration_execution()`: Specialized helpers used by copy-offload and multi-plan scenarios.

`conftest.py` does a lot of prep work before these helpers run. In particular, `prepared_plan` deep-copies the class config, clones or discovers source VMs, stores source-side facts in `source_vms_data`, creates configured hooks, and resolves `_vm_target_namespace` so later validation looks in the right namespace.

The plan config can drive much more than just warm versus cold migration. This warm migration example from `tests/tests_config/config.py` enables custom VM namespace placement, static IP preservation, PVC naming, labels, and affinity:

```434:466:tests/tests_config/config.py
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

In this repository, `None` under keys like `target_labels` or `target_node_selector` is a placeholder for “fill this with the current `session_uuid`,” which keeps parallel test runs from colliding.

The same module also supports copy-offload storage maps. When `datastore_id` and `offload_plugin_config` are passed, `get_storage_migration_map()` switches from inventory-derived mappings to explicit XCOPY-capable datastore mappings:

```84:112:tests/test_copyoffload_migration.py
copyoffload_config_data = source_provider_data["copyoffload"]
storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
datastore_id = copyoffload_config_data["datastore_id"]
storage_class = py_config["storage_class"]

vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]

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

For warm migrations, `execute_migration()` is often paired with `get_cutover_value()` from `utilities.migration_utils`, which computes the cutover time from `mins_before_cutover`.

### `utilities.hooks`

`utilities.hooks` lets plan configuration create pre- and post-migration Hook CRs without hand-writing YAML in every test. It supports two modes:

- `expected_result`: Use one of the built-in playbooks for a hook that should succeed or fail.
- `playbook_base64`: Supply your own base64-encoded Ansible playbook.

The module validates that you set exactly one of those options, and it rejects invalid base64, invalid UTF-8, invalid YAML, or playbooks that are not valid Ansible play lists.

A test scenario that intentionally keeps the migrated VM after a failing post hook is configured like this:

```503:515:tests/tests_config/config.py
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

`create_hook_if_configured()` stores the generated hook name and namespace back into the prepared plan as `_pre_hook_name`, `_pre_hook_namespace`, `_post_hook_name`, and `_post_hook_namespace`, so `create_plan_resource()` can pass them into the `Plan` CR.

`validate_hook_failure_and_check_vms()` is the helper that makes expected failures practical:

- If the migration failed in `PreHook`, it returns `False`, because the VM was never migrated.
- If the migration failed in `PostHook`, it returns `True`, because the VM may already exist and should still be validated.

> **Tip:** Use `expected_result` when you only need to exercise hook success or failure behavior. Switch to `playbook_base64` when the hook needs custom logic.

## Access and Validation

### `utilities.virtctl` and `utilities.ssh_utils`

This repository does not rely on node IP access for guest validation. Instead, it uses `virtctl port-forward` to reach a KubeVirt VM locally, then hands that tunnel to `python-rrmngmnt` for SSH operations.

`utilities.virtctl` makes that possible by locating or downloading a matching `virtctl` binary. It first checks whether `virtctl` is already in `PATH`, then checks for a previously downloaded copy, and only then falls back to downloading from the cluster’s `ConsoleCLIDownload` resource. The downloader knows how to match Linux and macOS builds and `x86_64` or `arm64` architectures.

`conftest.py` exposes this through the `virtctl_binary` fixture, which caches the binary in a cluster-versioned shared temp directory and uses a file lock so parallel `pytest-xdist` workers do not all download it at once.

The actual SSH tunnel command is built in `VMSSHConnection.setup_port_forward()`:

```98:141:utilities/ssh_utils.py
virtctl_path = shutil.which("virtctl")
if not virtctl_path:
    raise RuntimeError(
        "virtctl command not found in PATH. "
        "Please install virtctl before running the test suite. "
        "See README.md for installation instructions."
    )

if local_port is None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        local_port = s.getsockname()[1]

cmd = [
    virtctl_path,
    "port-forward",
    f"vm/{self.vm.name}",
    f"{local_port}:22",
    "--namespace",
    self.vm.namespace,
    "--address",
    "127.0.0.1",
]

if self.ocp_api_server:
    cmd.extend(["--server", self.ocp_api_server])

if self.ocp_token:
    cmd.extend(["--token", self.ocp_token])

if self.ocp_insecure:
    cmd.append("--insecure-skip-tls-verify")

cmd.extend(["-v", "3"])

cmd_str = " ".join(cmd)
if self.ocp_token:
    cmd_str = cmd_str.replace(self.ocp_token, "[REDACTED]")
LOGGER.info(f"Full virtctl command: {cmd_str}")
```

`SSHConnectionManager` is the higher-level wrapper most tests use. It creates VM connections through the destination provider, extracts the OpenShift API token on demand, and keeps track of all open connections so fixture teardown can close them cleanly.

> **Note:** This port-forward approach means SSH validation works even when worker nodes do not have public IPs.

The actual guest credentials come from `.providers.json`:

- `guest_vm_linux_user` and `guest_vm_linux_password` for Linux guests
- `guest_vm_win_user` and `guest_vm_win_password` for Windows guests

> **Warning:** `check_vms()` only tries SSH when the destination VM is powered on.

### `utilities.post_migration`

`utilities.post_migration` is the high-level “did the migration really work?” module. Its main entry point, `check_vms()`, looks up the source and destination VM objects, runs a broad set of focused validators, aggregates failures per VM, and only fails at the end. That gives you a much fuller error picture than stopping at the first failed assertion.

Depending on provider type and plan options, `check_vms()` can verify:

- Power state, CPU, and memory
- Network and storage mappings
- PVC names from `pvcNameTemplate`
- Snapshot preservation for vSphere
- BIOS serial preservation for vSphere, including the OCP 4.20+ format change
- Guest agent availability
- SSH connectivity
- Static IP preservation
- Node placement
- VM labels
- Affinity
- Provider secret SSL settings versus `source_provider_insecure_skip_verify`

A few behaviors matter in practice:

- `check_vms()` uses `plan["_vm_target_namespace"]`, so custom `vm_target_namespace` settings work automatically.
- `check_pvc_names()` understands Go-template-style `pvcNameTemplate` values, including `{{.VmName}}`, `{{.DiskIndex}}`, `{{.FileName}}`, and Sprig functions. If `pvc_name_template_use_generate_name` is `True`, it switches from exact matching to prefix matching.
- `check_ssl_configuration()` does a useful safety check: it compares the global source-provider SSL setting with the actual `insecureSkipVerify` value stored in the Provider secret.

> **Warning:** `check_static_ip_preservation()` is currently implemented only for Windows guests migrated from vSphere.

> **Tip:** Use `check_vms()` when you want the repository’s full standard validation bundle. If a test only cares about one behavior, calling a focused helper such as `check_vm_labels()` or `check_serial_preservation()` is often cleaner.

## Diagnostics and Teardown

### `utilities.must_gather`

`utilities.must_gather` is the repository’s failure-time diagnostic collector. It does not hardcode a must-gather image. Instead, it looks up the installed MTV `ClusterServiceVersion`, resolves the matching image digest mirror set, and builds the final image reference from the installed SHA. That keeps must-gather aligned with the cluster’s actual operator version.

When a plan is known, it runs targeted collection:

```166:181:utilities/must_gather.py
must_gather_image = _resolve_must_gather_image(
    ocp_admin_client=ocp_admin_client,
    mtv_subs=mtv_subs,
    mtv_csv=mtv_csv,
)

_must_gather_base_cmd = f"oc adm must-gather --image={must_gather_image} --dest-dir={data_collector_path}"

if plan:
    plan_name = plan["name"]
    plan_namespace = plan["namespace"]
    run_command(
        shlex.split(f"{_must_gather_base_cmd} -- NS={plan_namespace} PLAN={plan_name} /usr/bin/targeted")
    )
else:
    run_command(shlex.split(f"{_must_gather_base_cmd} -- -- NS={mtv_namespace}"))
```

That targeted mode is especially useful when a single migration plan failed and you want operator-side data for that specific plan instead of a much broader dump.

Errors in `run_must_gather()` are logged, but the helper does not crash the whole test run just because diagnostics collection failed.

### `utilities.pytest_utils` and `utilities.migration_utils`

These two modules are the cleanup and safety-net layer.

`utilities.pytest_utils.session_teardown()` is the top-level cleanup entry point. It cancels running migrations, archives plans, then hands off to the deeper resource deletion logic:

```107:128:utilities/pytest_utils.py
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

From there, the cleanup path does a few important things:

- `collect_created_resources()` writes the tracked resource list to `resources.json` under the data collector path.
- `teardown_resources()` deletes tracked `Migration`, `Plan`, `Provider`, `Secret`, `StorageMap`, `NetworkMap`, `Namespace`, and other resources.
- `cancel_migration()` cancels only migrations that are still running.
- `archive_plan()` marks plans as archived and waits for plan-owned pods to disappear.
- `check_dv_pvc_pv_deleted()` waits for `DataVolume`, `PersistentVolumeClaim`, and `PersistentVolume` cleanup in parallel.
- `pytest_exception_interact` and session-finish hooks call `run_must_gather()` when data collection is enabled and failures or leftovers justify it.

There is also a nearer, class-scoped cleanup path in `conftest.py`: `cleanup_migrated_vms` deletes migrated VMs after each test class finishes, including cases where VMs were intentionally migrated into a custom namespace. Session teardown is the backstop if anything survives beyond that.

> **Warning:** `--skip-teardown` is a debugging tool, not a normal operating mode. It leaves migrated VMs and tracked resources behind on purpose.

## Configuration Notes

A few configuration points affect these modules over and over:

- Global session settings in `tests/tests_config/config.py` include `insecure_verify_skip`, `source_provider_insecure_skip_verify`, `snapshots_interval`, `mins_before_cutover`, and `plan_wait_timeout`.
- Per-test entries in `tests/tests_config/config.py` control feature behavior with keys such as `warm_migration`, `target_power_state`, `preserve_static_ips`, `vm_target_namespace`, `pvc_name_template`, `pvc_name_template_use_generate_name`, `target_labels`, `target_affinity`, `target_node_selector`, `pre_hook`, and `post_hook`.
- Provider-specific connection details, guest OS credentials, and copy-offload settings come from `.providers.json`.

> **Tip:** If you are adding a new migration scenario, start by reusing `get_storage_migration_map()`, `get_network_migration_map()`, `create_plan_resource()`, `execute_migration()`, and `check_vms()`. That path matches the rest of the repository and gives you automatic teardown, SSH validation, and diagnostics with very little extra code.
