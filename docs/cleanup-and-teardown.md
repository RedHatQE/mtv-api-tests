# Cleanup And Teardown

`mtv-api-tests` cleans up automatically in normal runs. The project uses two cleanup layers: class-level cleanup for migrated VMs, and session-level cleanup for the rest of the resources the run created. If you need to stop that behavior for debugging, `--skip-teardown` preserves the environment so you can inspect it manually.

## Default Behavior

A standard run does all of the following:

- Tracks created resources as they are created.
- Removes migrated `VirtualMachine` objects when each test class finishes.
- Runs a broader session teardown at the end of pytest.
- Writes a resource inventory to `.data-collector/resources.json` unless you disable the data collector.

Resource tracking starts in `utilities/resources.py`:

```python
LOGGER.info(f"Storing {_resource.kind} {_resource.name} in fixture store")
_resource_dict = {"name": _resource.name, "namespace": _resource.namespace, "module": _resource.__module__}

if test_name:
    _resource_dict["test_name"] = test_name

fixture_store["teardown"].setdefault(_resource.kind, []).append(_resource_dict)
```

Anything created through `create_and_store_resource()` is registered automatically, which is why teardown can later find it again without guessing names or namespaces.

Not all cleanup waits until session end. Some fixture-scoped helpers clean up immediately after use. For example, SSH test connections are closed with `cleanup_all()`, copy-offload SSH keys are removed after the fixture yields, and temporary cluster edits made with `ResourceEditor` are reverted automatically when their context exits.

## Automatic Cleanup Flow

### Per-class VM cleanup

The standard class-based tests opt into VM cleanup explicitly. From `tests/test_mtv_cold_migration.py`:

```python
@pytest.mark.usefixtures("cleanup_migrated_vms")
class TestSanityColdMtvMigration:
    """Cold migration test - sanity check."""
```

That fixture runs after the class completes. From `conftest.py`:

```python
yield

if request.config.getoption("skip_teardown"):
    LOGGER.info("Skipping VM cleanup due to --skip-teardown flag")
    return

vm_namespace = prepared_plan.get("_vm_target_namespace", target_namespace)

for vm in prepared_plan["virtual_machines"]:
    vm_name = vm["name"]
    vm_obj = VirtualMachine(
        client=ocp_admin_client,
        name=vm_name,
        namespace=vm_namespace,
    )
    if vm_obj.exists:
        LOGGER.info(f"Cleaning up migrated VM: {vm_name} from namespace: {vm_namespace}")
        vm_obj.clean_up()
```

A few practical details come from that logic:

- `cleanup_migrated_vms` is teardown-only. It does not set anything up; it just removes migrated VMs after the class.
- The same `--skip-teardown` flag disables this VM cleanup too.
- If the plan migrated into a custom `vm_target_namespace`, that namespace is used automatically.

### Session teardown

At the end of the session, `conftest.py` writes the resource inventory and then decides whether to run teardown:

```python
if not session.config.getoption("skip_data_collector"):
    collect_created_resources(session_store=_session_store, data_collector_path=_data_collector_path)

if session.config.getoption("skip_teardown"):
    LOGGER.warning("User requested to skip teardown of resources")

else:
    try:
        session_teardown(session_store=_session_store)
    except Exception as exp:
        LOGGER.error(f"the following resources was left after tests are finished: {exp}")
        if not session.config.getoption("skip_data_collector"):
            run_must_gather(data_collector_path=_data_collector_path)
```

The session teardown in `utilities/pytest_utils.py` starts by cancelling active migrations and archiving plans:

```python
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

From there, `teardown_resources()` works through the rest of the inventory. In practice, the session-level sweep covers:

- `Migration` and `Plan` resources.
- `Provider`, `Secret`, and `Host` resources.
- `NetworkAttachmentDefinition`, `StorageMap`, and `NetworkMap` resources.
- Tracked `VirtualMachine` and `Pod` resources.
- Namespaces created during the run.
- Source-side cloned VMs for VMware, OpenStack, and RHV.
- OpenStack volume snapshots that were recorded during clone preparation.

It also performs extra cleanup and verification in the target namespace by:

- Deleting any remaining VMs with `delete_all_vms()`.
- Waiting for matching pods to disappear.
- Waiting for matching `DataVolume`, `PersistentVolumeClaim`, and `PersistentVolume` objects to be deleted.

> **Note:** `.data-collector/resources.json` is written before session teardown runs. That means the file is available both when you use `--skip-teardown` and when teardown later reports a problem.

### Leftover detection

Teardown is more than a best-effort delete loop. The code explicitly tracks leftovers and raises `SessionTeardownError` if resources are still present after cleanup attempts.

That leftover detection is especially important for migration side effects such as pods, PVCs, and PVs. The session code looks for objects tied to the current run’s session UUID and records anything that did not disappear cleanly.

If the data collector is enabled and teardown hits a problem, the session then runs MTV `must-gather` to capture diagnostics in the same collector path.

> **Warning:** Leftover teardown problems are currently surfaced through session-finish logging. `pytest_sessionfinish()` logs the teardown exception and can trigger `must-gather`, but it does not re-raise that exception after logging it. Always check the end-of-run output, not just the individual test results.

## Debugging With `--skip-teardown`

The user-facing flag lives in `conftest.py`:

```python
teardown_group.addoption(
    "--skip-teardown", action="store_true", help="Do not teardown resource created by the tests"
)
```

The data-collector path is configurable too:

```python
data_collector_group.addoption(
    "--data-collector-path", help="Path to store collected data for failed tests", default=".data-collector"
)
```

A repository example from `docs/copyoffload/how-to-run-copyoffload-tests.md` shows the intended usage inside a job command:

```yaml
# In the Job command section, add --skip-teardown:
uv run pytest -m copyoffload --skip-teardown \
  -v \
  ...
```

Use `--skip-teardown` when you want to inspect the environment after a run, for example:

- The migrated VMs that were created in the target namespace.
- The `Plan`, `Migration`, `StorageMap`, and `NetworkMap` objects.
- The pods, PVCs, DataVolumes, and provider-side clones that would normally be removed automatically.

> **Warning:** `--skip-teardown` disables both cleanup layers. The class-level `cleanup_migrated_vms` fixture returns early, and the end-of-session `session_teardown()` call is skipped entirely.

> **Tip:** If you keep resources for debugging, do not also use `--skip-data-collector` unless you truly want no tracking artifacts. Leaving the data collector enabled gives you `.data-collector/resources.json`, which is the easiest input for follow-up cleanup.

## Manual Cleanup Helpers

### Use the recorded resource inventory

When the data collector is enabled, the run writes a JSON inventory of the resources it created. From `utilities/pytest_utils.py`:

```python
if resources:
    try:
        LOGGER.info(f"Write created resources data to {data_collector_path}/resources.json")
        with open(data_collector_path / "resources.json", "w") as fd:
            json.dump(session_store["teardown"], fd)
```

The repository includes a standalone helper script for replaying that inventory. From `tools/clean_cluster.py`:

```python
def clean_cluster_by_resources_file(resources_file: str) -> None:
    with open(resources_file, "r") as fd:
        data: dict[str, list[dict[str, str]]] = json.load(fd)

    for _resource_kind, _resources_list in data.items():
        for _resource in _resources_list:
            _resource_module = importlib.import_module(_resource["module"])
            _resource_class = getattr(_resource_module, _resource_kind)
            _kwargs = {"name": _resource["name"]}
            if _resource.get("namespace"):
                _kwargs["namespace"] = _resource["namespace"]

            _resource_class(**_kwargs).clean_up()
```

The CLI entrypoint shows the expected usage format:

```python
if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python clean_cluster.py <resources_file>")
        sys.exit(1)

    clean_cluster_by_resources_file(resources_file=sys.argv[1])
```

With the default collector path, the input file is `.data-collector/resources.json`. If you used `--data-collector-path`, point the script at that directory’s `resources.json` instead. If you ran with `--skip-data-collector`, the helper has no inventory file to consume, and teardown failures will not trigger `must-gather`.

> **Note:** `resources.json` is a record of what the session created, not a leftovers-only report. In a successful run, some or all of those resources may already be gone by the time you inspect the file.

> **Warning:** `tools/clean_cluster.py` is best suited to OpenShift-side resources recorded through `create_and_store_resource()`, because it recreates objects from the stored `module` and resource kind. Provider-side clone cleanup for VMware/OpenStack/RHV and OpenStack volume snapshots is handled by the full session teardown logic in `utilities/pytest_utils.py`, not by this standalone helper.

### Use the session name to find leftovers

When you need to clean up manually in OpenShift or in the source provider UI/CLI, the run-specific session UUID is your most useful search key.

From `conftest.py`:

```python
def session_uuid(fixture_store):
    _session_uuid = generate_name_with_uuid(name="auto")
    fixture_store["session_uuid"] = _session_uuid
    return _session_uuid
```

The main target namespace is built from that same session value:

```python
unique_namespace_name = f"{session_uuid}{_target_namespace}"[:63]
fixture_store["target_namespace"] = unique_namespace_name
```

Source-provider clones are named the same way. From `libs/base_provider.py`:

```python
clone_vm_name = generate_name_with_uuid(f"{session_uuid}-{base_name}")
```

That means a single session prefix, typically something like `auto-ab12`, often shows up in all of these places:

- The auto-created target namespace.
- Auto-generated OpenShift resource names.
- Source-provider clone names.

> **Tip:** If you skipped teardown, start by finding the session prefix from the run logs or from `.data-collector/resources.json`, then search OpenShift and the source provider for that same prefix.

> **Note:** Some tests use a custom `vm_target_namespace`. In those cases, manual cleanup needs to check that namespace too, because migrated VMs and their storage objects may live there instead of the default session target namespace. OpenShift-source runs can also create a `source_vms_namespace` named `<session_uuid>-source-vms`, so check that namespace as well when cleaning manually.
