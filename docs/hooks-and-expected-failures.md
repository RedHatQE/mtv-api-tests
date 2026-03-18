# Hooks And Expected Failures

Hooks let you run Ansible logic before or after an MTV migration. In this repository, you do not hand-craft `Hook` resources yourself. You describe the hook in the plan configuration, and the test suite creates the `Hook` resource, attaches it to the `Plan`, and validates the outcome.

The project supports two hook modes:

- predefined playbooks selected with `expected_result`
- custom playbooks supplied through `playbook_base64`

When you intentionally test a failure, the suite validates more than “migration failed.” It checks whether the failure happened in `PreHook` or `PostHook`, and it changes later VM validation based on that result.

## Where Hook Configuration Lives

Hook settings live in `tests/tests_config/config.py`. The repository’s end-to-end hook example looks like this:

```503:516:tests/tests_config/config.py
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

Use `pre_hook` when you want to affect the migration before VM work begins. Use `post_hook` when you want the migration to reach the end of VM processing and then test what happens after that.

> **Note:** `expected_result` and `expected_migration_result` are not interchangeable. The first controls hook behavior. The second controls the expected result of the entire migration test.

## How Hooks Get Attached To A Plan

During `prepared_plan`, the suite checks the plan for `pre_hook` and `post_hook`. If either exists, it creates the corresponding `Hook` resource and stores the generated name and namespace back into the plan.

```306:334:utilities/hooks.py
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

When the suite creates the MTV `Plan`, it passes those hook references into the Plan helper. Pre-hooks use `pre_hook_name` and `pre_hook_namespace`. Post-hooks are passed through the helper as `after_hook_name` and `after_hook_namespace`.

```200:223:utilities/mtv_migration.py
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
        "virtual_machines_list": virtual_machines_list,
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

> **Note:** You do not configure hook resource names manually in the test config. The suite generates them and stores them as `_pre_hook_name`, `_pre_hook_namespace`, `_post_hook_name`, and `_post_hook_namespace`.

> **Note:** Hook resources are created in the migration namespace passed as `target_namespace`. If you also use `vm_target_namespace`, that changes where migrated VMs land, not where the hook CR itself is created.

## Predefined Playbooks

If you set `expected_result`, the suite chooses one of two built-in Ansible playbooks stored as base64 strings in `utilities/hooks.py`. The file includes their decoded content in comments:

```29:43:utilities/hooks.py
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

> **Tip:** If your goal is to verify that a migration fails specifically in `PreHook` or `PostHook`, predefined playbooks are the clearest option because the test can compare the actual failed step against a declared expectation.

## Custom Playbooks

If the built-in success and failure playbooks are not enough, you can provide your own playbook in `playbook_base64`.

Before the suite creates the hook, it enforces several validation rules:

```74:130:utilities/hooks.py
    expected_result = hook_config.get("expected_result")
    custom_playbook = hook_config.get("playbook_base64")

    # Validate mutual exclusivity
    if expected_result is not None and custom_playbook is not None:
        raise ValueError(
            f"Invalid {hook_type} hook config: 'expected_result' and 'playbook_base64' are "
            f"mutually exclusive. Use 'expected_result' for predefined playbooks, or "
            f"'playbook_base64' for custom playbooks."
        )

    if expected_result is None and custom_playbook is None:
        raise ValueError(
            f"Invalid {hook_type} hook config: must specify either 'expected_result' or 'playbook_base64'."
        )

    # Reject empty strings for both expected_result and custom_playbook
    if isinstance(expected_result, str) and expected_result.strip() == "":
        raise ValueError(f"Invalid {hook_type} hook config: 'expected_result' cannot be empty or whitespace-only.")

    if isinstance(custom_playbook, str) and custom_playbook.strip() == "":
        raise ValueError(f"Invalid {hook_type} hook config: 'playbook_base64' cannot be empty or whitespace-only.")
    ...
    # Validate base64 encoding
    try:
        decoded = base64.b64decode(playbook_base64, validate=True)
    except binascii.Error as e:
        raise ValueError(f"Invalid {hook_type} hook playbook_base64: not valid base64 encoding. Error: {e}") from e

    ...
    # Validate Ansible playbook structure (must be a non-empty list)
    if not isinstance(playbook_data, list) or not playbook_data:
        raise ValueError(
            f"Invalid {hook_type} hook playbook_base64: Ansible playbook must be a non-empty list of plays"
        )
```

A custom hook payload must therefore be:

- valid base64
- valid UTF-8 after decoding
- valid YAML
- a non-empty list of plays

> **Warning:** `expected_result` and `playbook_base64` are mutually exclusive. You must supply exactly one of them for each hook.

> **Warning:** Passing validation only proves the payload is structurally valid. It does not guarantee the playbook will succeed at runtime.

> **Note:** This repository has an end-to-end example for predefined hooks, but it does not currently include a dedicated sample test case that uses `playbook_base64`.

## How Expected Failures Are Validated

A correct hook-failure scenario does not show up as a broken test in this suite. The migration itself fails, but the pytest test passes because that failure was expected and was validated.

The existing hook test does that explicitly. If `expected_migration_result` is `fail`, it expects `execute_migration()` to raise `MigrationPlanExecError`, and then it asks the hook utility whether VM checks should still run.

```195:246:tests/test_post_hook_retain_failed_vm.py
        expected_result = prepared_plan["expected_migration_result"]

        if expected_result == "fail":
            with pytest.raises(MigrationPlanExecError):
                execute_migration(
                    ocp_admin_client=ocp_admin_client,
                    fixture_store=fixture_store,
                    plan=self.plan_resource,
                    target_namespace=target_namespace,
                )
            self.__class__.should_check_vms = validate_hook_failure_and_check_vms(self.plan_resource, prepared_plan)
        else:
            execute_migration(
                ocp_admin_client=ocp_admin_client,
                fixture_store=fixture_store,
                plan=self.plan_resource,
                target_namespace=target_namespace,
            )
            self.__class__.should_check_vms = True
    ...
        # Runtime skip needed - decision based on previous test's migration execution result
        if not self.__class__.should_check_vms:
            pytest.skip("Skipping VM checks - hook failed before VM migration")
```

The suite then looks deeper than the high-level `Plan` status. It locates the related `Migration` resource and scans each VM’s pipeline for the first step with an error.

```55:99:utilities/mtv_migration.py
def _get_failed_migration_step(plan: Plan, vm_name: str) -> str:
    """Get step where VM migration failed.

    Examines the Migration status (not Plan) to find which pipeline step failed.
    The Migration CR contains the detailed VM pipeline execution status.
    """
    migration = _find_migration_for_plan(plan)

    if not hasattr(migration.instance, "status") or not migration.instance.status:
        raise MigrationStatusError(migration_name=migration.name)

    vms_status = getattr(migration.instance.status, "vms", None)
    if not vms_status:
        raise MigrationStatusError(migration_name=migration.name)

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
```

Once it knows the actual failed step, the hook utility validates it against the configured hook and decides what to do next:

```223:303:utilities/hooks.py
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

def validate_hook_failure_and_check_vms(
    plan_resource: "Plan",
    prepared_plan: dict[str, Any],
) -> bool:
    ...
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

> **Warning:** For multi-VM plans, the suite expects all VMs to fail in the same step. If different VMs fail in different pipeline steps, validation fails with `VmMigrationStepMismatchError`.

> **Note:** In custom-playbook mode, the suite still determines whether the failure happened in `PreHook` or `PostHook`. What it skips is the comparison against a declared expected step, because custom mode does not use `expected_result`.

## Pytest And Reporting Behavior

These hook tests use the repository’s standard incremental class pattern. If an earlier step in the class fails unexpectedly, later steps are marked `xfail` instead of running anyway.

```123:180:conftest.py
    # Incremental test support - track failures for class-based tests
    if "incremental" in item.keywords and rep.when == "call" and rep.failed:
        item.parent._previousfailed = item
    ...
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

The test runner is also configured to write `junit-report.xml`, so correctly validated hook-failure scenarios appear as normal passed or skipped test steps in standard reporting. The migration failed, but the test did exactly what it was supposed to do.
