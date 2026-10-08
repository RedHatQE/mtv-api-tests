# Hooks And Expected Failures

Hooks let you run Ansible logic before or after an MTV migration. In this repository, you do not hand-craft `Hook` resources yourself. You describe the hook in the plan
configuration, and the test suite creates the `Hook` resource, attaches it to the `Plan`, and validates the outcome.

The project supports three hook modes, and each `pre_hook` or `post_hook` block uses exactly one of them:

- predefined playbooks selected with `expected_result`
- custom playbooks supplied through `playbook_base64`
- AAP (Ansible Automation Platform) job templates selected with `aap_job_template_id`

When you intentionally test a failure, the suite validates more than “migration failed.” It checks whether the failure happened in `PreHook` or `PostHook`, and it changes later VM
validation based on that result.

## Hook Test Classes

| Test class | Where | Markers | What it covers |
| --- | --- | --- | --- |
| `TestPostHookRetainFailedVm` | `tests/hooks/test_post_hook_retain_failed_vm.py` | `tier0`, `incremental` | Predefined hooks, post hook fails on purpose, VMs retained |
| `TestAapHookMigration` | `tests/hooks/test_aap_hook_migration.py` | `vsphere`, `tier1`, `aap`, `incremental` | AAP hooks: AWX job templates run as PreHook and PostHook |

`TestPostHookRetainFailedVm` also carries `vsphere`, `rhv`, `openstack`, and `openshift`, because it
is not tied to one source type.

The `aap` marker is the one to reach for when you only want the AAP integration test:

```bash
uv run pytest -m aap -v --tc=source_provider:vsphere-8.0.3.00400
```

## Where Hook Configuration Lives

Hook settings live in `tests/tests_config/config.py`. The repository’s end-to-end hook example looks like this:

```743:756:tests/tests_config/config.py
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

This config uses two different “expected” keys:

| Key | What it controls |
| --- | --- |
| `pre_hook.expected_result` / `post_hook.expected_result` | Chooses a built-in hook playbook: `succeed` or `fail`. |
| `expected_migration_result` | Tells the test whether the overall migration should raise `MigrationPlanExecError`. |

Use `pre_hook` when you want to affect the migration before VM work begins. Use `post_hook` when you want the migration to reach the end of VM processing and then test what happens
after that.

One other plan config in the repository also sets a failing post hook: `test_plan_archive_pvc_cleanup`.
That test does not use `expected_migration_result` at all. It wraps `execute_migration()` in
`pytest.raises(MigrationPlanExecError)` directly, then archives and deletes the `Plan` to verify the
leftover `DataVolume` and PVC cleanup.

> **Note:** `expected_result` and `expected_migration_result` are not interchangeable. The first controls hook behavior. The second controls the expected result of the entire
> migration test.

## How Hooks Get Attached To A Plan

During `prepared_plan`, the suite checks the plan for `pre_hook` and `post_hook`. If either exists, it creates the corresponding `Hook` resource and stores the generated name and
namespace back into the plan.

```335:363:utilities/hooks.py
def create_hook_if_configured(
    plan: dict[str, Any],
    hook_key: str,
    hook_type: str,
    fixture_store: dict[str, Any],
    ocp_admin_client: "DynamicClient",
    target_namespace: str,
) -> None:
    """Create hook if configured in plan and store references.

    ...
    """
    hook_config = plan.get(hook_key)
    if hook_config:
        hook_name, hook_namespace = create_hook_for_plan(
            hook_config=hook_config,
            hook_type=hook_type,
            fixture_store=fixture_store,
            ocp_admin_client=ocp_admin_client,
            target_namespace=target_namespace,
        )
        plan[f"_{hook_type}_hook_name"] = hook_name
        plan[f"_{hook_type}_hook_namespace"] = hook_namespace
```

The `prepared_plan` fixture calls this once for `pre_hook` and once for `post_hook`. When the suite
creates the MTV `Plan`, it passes those hook references into the Plan helper. Pre-hooks use
`pre_hook_name` and `pre_hook_namespace`. Post-hooks are passed through the helper as
`after_hook_name` and `after_hook_namespace`.

```275:305:utilities/mtv_migration.py
    for vm in vms_for_plan:
        if "migrate_shared_disks" in vm:
            vm["migrateSharedDisks"] = vm.pop("migrate_shared_disks")
        for key in _VM_DICT_TEST_ONLY_KEYS:
            vm.pop(key, None)

    plan_kwargs: dict[str, Any] = {
        "client": ocp_admin_client,
        "fixture_store": fixture_store,
        "resource": Plan,
        "namespace": target_namespace,
        "source_provider_name": source_provider.ocp_resource.name,
        "source_provider_namespace": source_provider.ocp_resource.namespace,
        "destination_provider_name": destination_provider.ocp_resource.name,
        "destination_provider_namespace": destination_provider.ocp_resource.namespace,
        "storage_map_name": storage_map.name,
        "storage_map_namespace": storage_map.namespace,
        "network_map_name": network_map.name,
        "network_map_namespace": network_map.namespace,
        "virtual_machines_list": vms_for_plan,
        "target_namespace": vm_target_namespace or target_namespace,
        "warm_migration": warm_migration,
        "pre_hook_name": pre_hook_name,
        "pre_hook_namespace": pre_hook_namespace,
        "after_hook_name": after_hook_name,
        "after_hook_namespace": after_hook_namespace,
        "preserve_static_ips": preserve_static_ips,
        "pvc_name_template": pvc_name_template,
        "pvc_name_template_use_generate_name": pvc_name_template_use_generate_name,
        "target_power_state": target_power_state,
    }
```

Both hook test classes then read the same four keys back out of `prepared_plan` when they build their `Plan`.

> **Note:** You do not configure hook resource names manually in the test config. The suite generates them and stores them as `_pre_hook_name`, `_pre_hook_namespace`,
> `_post_hook_name`, and `_post_hook_namespace`.

> **Note:** Hook resources are created in the migration namespace passed as `target_namespace`. If you also use `vm_target_namespace`, that changes where migrated VMs land, not
> where the hook CR itself is created.

## Predefined Playbooks

If you set `expected_result`, the suite chooses one of two built-in Ansible playbooks stored as base64 strings in `utilities/hooks.py`. The file includes their decoded content in
comments:

```27:43:utilities/hooks.py
# Predefined hook playbooks for testing (base64 encoded Ansible playbooks)
#
# HOOK_PLAYBOOK_SUCCESS decodes to:
# - name: Successful-hook
#   hosts: localhost
#   tasks:
#     - name: Success task
#       debug:
#         msg: "Hook executed successfully"
#
# HOOK_PLAYBOOK_FAIL decodes to:
# - name: Failing-post-migration
#   hosts: localhost
#   tasks:
#     - name: Task that will fail
#       fail:
#         msg: "This hook is designed to fail for testing purposes"
```

In other words:

- `expected_result: succeed` uses a simple playbook that logs a debug message.
- `expected_result: fail` uses a playbook that fails on purpose.

This makes predefined mode the easiest way to test hook behavior without having to build and base64-encode your own playbook.

> **Tip:** If your goal is to verify that a migration fails specifically in `PreHook` or `PostHook`, predefined playbooks are the clearest option because the test can compare the
> actual failed step against a declared expectation.

## Custom Playbooks

If the built-in success and failure playbooks are not enough, you can provide your own playbook in `playbook_base64`.

Before the suite creates the hook, it enforces several validation rules:

```74:110:utilities/hooks.py
    expected_result = hook_config.get("expected_result")
    custom_playbook = hook_config.get("playbook_base64")
    aap_job_template_id = hook_config.get("aap_job_template_id")

    modes_specified = sum(x is not None for x in (expected_result, custom_playbook, aap_job_template_id))

    if modes_specified > 1:
        raise ValueError(
            f"Invalid {hook_type} hook config: 'expected_result', 'playbook_base64', and "
            f"'aap_job_template_id' are mutually exclusive. Specify exactly one."
        )

    if modes_specified == 0:
        raise ValueError(
            f"Invalid {hook_type} hook config: must specify exactly one of 'expected_result', "
            f"'playbook_base64', or 'aap_job_template_id'."
        )

    if aap_job_template_id is not None:
        if (
            isinstance(aap_job_template_id, bool)
            or not isinstance(aap_job_template_id, int)
            or aap_job_template_id <= 0
        ):
            raise ValueError(
                f"Invalid {hook_type} hook config: 'aap_job_template_id' must be a positive integer, "
                f"got: {aap_job_template_id!r}"
            )

    # Reject empty strings for both expected_result and custom_playbook
    if isinstance(expected_result, str) and expected_result.strip() == "":
        raise ValueError(f"Invalid {hook_type} hook config: 'expected_result' cannot be empty or whitespace-only.")

    if isinstance(custom_playbook, str) and custom_playbook.strip() == "":
        raise ValueError(f"Invalid {hook_type} hook config: 'playbook_base64' cannot be empty or whitespace-only.")
```

A custom hook payload must therefore be:

- valid base64
- valid UTF-8 after decoding
- valid YAML
- a non-empty list of plays

> **Warning:** `expected_result`, `playbook_base64`, and `aap_job_template_id` are mutually exclusive. You must supply exactly one of them for each hook.

> **Warning:** Passing validation only proves the payload is structurally valid. It does not
> guarantee the playbook will succeed at runtime.

> **Note:** This repository has an end-to-end example for predefined hooks and one for AAP hooks, but
> it does not currently include a test case whose plan config uses `playbook_base64`.

## AAP Hooks

The third mode runs the hook as an AWX job template instead of embedding a playbook in the `Hook` CR.
Instead of `image` and `playbook`, the suite creates the `Hook` with an `aap` block:

```python
hook = create_and_store_resource(
    client=ocp_admin_client,
    fixture_store=fixture_store,
    resource=Hook,
    namespace=target_namespace,
    aap={"jobTemplateId": aap_job_template_id},
)
```

`TestAapHookMigration` never writes `aap_job_template_id` into `tests/tests_config/config.py`.
Its plan config is a plain cold migration. The fixtures in `tests/hooks/conftest.py` build everything
else at runtime:

- `awx_deployment` installs the AWX operator via Helm, creates an AWX instance backed by CephFS
  storage classes, waits for all pods, and returns the AWX route URL. If AWX was already present,
  the fixture leaves it in place at teardown instead of deleting it.
- `awx_api_token` creates an AWX OAuth2 API token for the admin user.
- `awx_job_templates` creates a project from the `mtv-aap-test-playbooks` git repository, waits for
  SCM sync, creates an inventory, then creates the `mtv-pre-hook` and `mtv-post-hook` job templates
  and returns their IDs.
- `aap_mtv_settings` creates a session-unique token `Secret` in the MTV namespace and patches the
  `ForkliftController` with three fields — `spec.aap_url`, `spec.aap_token_secret_name`, and
  `spec.aap_insecure_skip_verify`:
- `aap_hook_refs` creates one `Hook` per type with `spec.aap.jobTemplateId` pointing at the matching
  template ID, then writes `_pre_hook_name`, `_pre_hook_namespace`, `_post_hook_name`, and
  `_post_hook_namespace` into `prepared_plan` — the same four keys the predefined mode writes.

```python
editor = ResourceEditor(
    patches={
        forklift_controller: {
            "spec": {
                "aap_url": awx_deployment,
                "aap_token_secret_name": token_secret_name,
                "aap_insecure_skip_verify": "true",
            }
        }
    }
)
editor.update(backup_resources=True)
```

The patch is restored during pytest session teardown, not when the test class finishes: `aap_mtv_settings` is session-scoped
(`tests/hooks/conftest.py:163`) and calls `editor.restore()` in its fixture teardown, so MTV keeps its AAP configuration for the
rest of the run.

The migration step of `TestAapHookMigration` asserts success, not failure. Because the hooks run in
pipeline order, a passing migration is what proves Forklift launched the AWX jobs and waited for
them: PreHook runs before disk transfer, PostHook after VM creation.

> **Warning:** The AAP path mutates cluster-wide MTV settings and installs an AWX instance in the `awx`
> namespace. It needs `ocs-storagecluster-cephfs` to exist as a storage class for the AWX project and
> PostgreSQL volumes.
>
> **Tip:** If AWX is already deployed in the cluster, the fixture reuses it and skips teardown, so a
> shared AWX is not torn down when the test class ends.

## How Expected Failures Are Validated

A correct hook-failure scenario does not show up as a broken test in this suite. The migration itself fails, but the pytest test passes because that failure was expected and was
validated.

The expected-failure test does that explicitly. If `expected_migration_result` is `fail`, it expects
`execute_migration()` to raise `MigrationPlanExecError`, and then it asks the hook utility whether VM
checks should still run.

```199:223:tests/hooks/test_post_hook_retain_failed_vm.py
        expected_result = prepared_plan["expected_migration_result"]

        if expected_result == "fail":
            with pytest.raises(MigrationPlanExecError) as exc_info:
                execute_migration(
                    ocp_admin_client=ocp_admin_client,
                    fixture_store=fixture_store,
                    plan=self.plan_resource,
                    target_namespace=target_namespace,
                )
            try:
                self.__class__.should_check_vms = validate_hook_failure_and_check_vms(self.plan_resource, prepared_plan)
            except Exception as e:
                # Chain with original migration error so the root cause is visible in traceback
                e.__cause__ = exc_info.value
                raise
        else:
            execute_migration(
                ocp_admin_client=ocp_admin_client,
                fixture_store=fixture_store,
                plan=self.plan_resource,
                target_namespace=target_namespace,
            )
            self.__class__.should_check_vms = True
```

And the VM step turns that boolean into a skip:

```253:255:tests/hooks/test_post_hook_retain_failed_vm.py
        # Runtime skip needed - decision based on previous test's migration execution result
        if not self.__class__.should_check_vms:
            pytest.skip("Skipping VM checks - hook failed before VM migration")
```

The suite then looks past the high-level migration outcome. It reads each VM's pipeline from the
`Plan` CR status and returns the first step that carries an error. The `Plan` CR is the authoritative
source on purpose: the forklift controller writes it before the `Migration` CR syncs, so a migration
that just failed cannot race into a "no error found" result.

```81:103:utilities/mtv_migration.py
    vms_status = plan.instance.status.migration.vms

    for vm_status in vms_status:
        vm_id = getattr(vm_status, "id", "")
        vm_status_name = getattr(vm_status, "name", "")

        if vm_name not in (vm_id, vm_status_name):
            continue

        pipeline = getattr(vm_status, "pipeline", None)
        if not pipeline:
            raise VmPipelineError(vm_name=vm_name)

        for step in pipeline:
            step_error = getattr(step, "error", None)
            if step_error:
                step_name = step.name
                LOGGER.info(f"VM {vm_name} failed at step '{step_name}': {step_error}")
                return step_name

        raise VmPipelineError(vm_name=vm_name)

    raise VmNotFoundError(f"VM '{vm_name}' not found in Plan '{plan.name}' migration status")
```

A VM that is missing from the plan status raises `VmNotFoundError`; a VM with no pipeline or no failed step raises `VmPipelineError`.

Once it knows the actual failed step, the hook utility validates it against the configured hook and decides what to do next:

```252:294:utilities/hooks.py
def validate_expected_hook_failure(
    actual_failed_step: str,
    plan_config: dict[str, Any],
) -> None:
    """
    Validate the actual failed step matches expected (predefined mode only).

    For custom playbook mode (no expected_result set), this is a no-op.
    """
    # Extract hook configs with type validation
    pre_hook_config = plan_config.get("pre_hook")
    if pre_hook_config is not None and not isinstance(pre_hook_config, dict):
        raise TypeError(f"pre_hook must be a dict, got {type(pre_hook_config).__name__}")
    pre_hook_expected = pre_hook_config.get("expected_result") if pre_hook_config else None

    post_hook_config = plan_config.get("post_hook")
    if post_hook_config is not None and not isinstance(post_hook_config, dict):
        raise TypeError(f"post_hook must be a dict, got {type(post_hook_config).__name__}")
    post_hook_expected = post_hook_config.get("expected_result") if post_hook_config else None

    # PreHook runs before PostHook, so check PreHook first
    if pre_hook_expected == "fail":
        expected_step = "PreHook"
    elif post_hook_expected == "fail":
        expected_step = "PostHook"
    else:
        LOGGER.info("No expected_result specified - skipping step validation")
        return

    if actual_failed_step != expected_step:
        raise AssertionError(
            f"Migration failed at step '{actual_failed_step}' but expected to fail at '{expected_step}'"
        )

    LOGGER.info("Migration correctly failed at expected step '%s'", expected_step)
```

```322:333:utilities/hooks.py
    if actual_failed_step == "PostHook":
        return True
    elif actual_failed_step == "PreHook":
        return False
    else:
        raise ValueError(f"Unexpected failure step: {actual_failed_step}")
```

That leads to a simple rule set:

- If the actual failed step matches the expected hook, the expected-failure test passes.
- If the actual failed step is wrong, the test fails.
- If the failure happened in `PreHook`, VM checks are skipped because migration stopped too early.
- If the failure happened in `PostHook`, VM checks still run because the VMs should already exist.

> **Warning:** For multi-VM plans, the suite expects all VMs to fail in the same step.
> `validate_all_vms_same_step()` collects one failed step per VM name and raises
> `VmMigrationStepMismatchError` when the values differ, or when no VM produced a step at all.

> **Note:** In custom-playbook mode, the suite still determines whether the failure happened in `PreHook` or `PostHook`. What it skips is the comparison against a declared expected
> step, because custom mode does not use `expected_result`.

## Pytest And Reporting Behavior

Both hook test classes use the repository's standard incremental class pattern. If an earlier step in
the class fails unexpectedly, later steps are marked `xfail` instead of running anyway.

```159:161:conftest.py
    # Incremental test support - track failures for class-based tests
    if "incremental" in item.keywords and rep.when == "call" and rep.failed:
        item.parent._previousfailed = item
```

```223:228:conftest.py
def pytest_runtest_setup(item):
    # Incremental test support - xfail if previous test in class failed
    if "incremental" in item.keywords:
        previousfailed = getattr(item.parent, "_previousfailed", None)
        if previousfailed is not None:
            pytest.xfail(f"previous test failed ({previousfailed.name})")
```

This is different from hook expected-failure validation:

- `pytest.xfail()` is used here to stop later class steps after an unexpected earlier failure.
- Hook expected failures are validated explicitly with `pytest.raises(MigrationPlanExecError)` plus step checking.

> **Tip:** If a hook failure is part of the test’s intended behavior, model it with `expected_migration_result: fail` and hook-step validation, not with `pytest.xfail()`.

The test runner is also configured to write `junit-report.xml`, so correctly validated hook-failure scenarios appear as normal passed or skipped test steps in standard reporting.
The migration failed, but the test did exactly what it was supposed to do.
