"""
Hook validation and creation utilities for MTV migration testing.

This module provides functions to validate and create Forklift Hook CRs
for pre-migration and post-migration testing.
"""

from __future__ import annotations

import base64
import binascii
import logging
from typing import TYPE_CHECKING, Any

import yaml
from ocp_resources.hook import Hook

from utilities.resources import create_and_store_resource

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

LOGGER = logging.getLogger(__name__)

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
#
HOOK_PLAYBOOK_SUCCESS: str = "LSBuYW1lOiBTdWNjZXNzZnVsLWhvb2sKICBob3N0czogbG9jYWxob3N0CiAgdGFza3M6CiAgICAtIG5hbWU6IFN1Y2Nlc3MgdGFzawogICAgICBkZWJ1ZzoKICAgICAgICBtc2c6ICJIb29rIGV4ZWN1dGVkIHN1Y2Nlc3NmdWxseSI="  # pragma: allowlist secret

HOOK_PLAYBOOK_FAIL: str = "LSBuYW1lOiBGYWlsaW5nLXBvc3QtbWlncmF0aW9uCiAgaG9zdHM6IGxvY2FsaG9zdAogIHRhc2tzOgogICAgLSBuYW1lOiBUYXNrIHRoYXQgd2lsbCBmYWlsCiAgICAgIGZhaWw6CiAgICAgICAgbXNnOiAiVGhpcyBob29rIGlzIGRlc2lnbmVkIHRvIGZhaWwgZm9yIHRlc3RpbmcgcHVycG9zZXMi"  # pragma: allowlist secret


def validate_hook_config(hook_config: dict[str, Any], hook_type: str) -> None:
    """
    Validate hook configuration for mutual exclusivity and required fields.

    Args:
        hook_config (dict[str, Any]): Hook configuration dict
        hook_type (str): "pre" or "post" for error messages

    Raises:
        TypeError: If hook_config is not a dict
        ValueError: If expected_result and playbook_base64 are both specified,
                   or if neither is specified
    """
    if not isinstance(hook_config, dict):
        raise TypeError(f"Invalid {hook_type} hook config: expected dict, got {type(hook_config).__name__}")

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


def validate_custom_playbook(playbook_base64: str, hook_type: str) -> None:
    """
    Validate custom playbook base64 encoding, UTF-8, and YAML syntax.

    Args:
        playbook_base64 (str): Base64-encoded Ansible playbook
        hook_type (str): "pre" or "post" for error messages

    Raises:
        ValueError: If playbook is invalid base64, UTF-8, YAML, or not a valid Ansible playbook
    """
    # Validate base64 and UTF-8
    try:
        decoded = base64.b64decode(playbook_base64, validate=True)
        playbook_text = decoded.decode("utf-8")
    except (binascii.Error, ValueError) as e:
        raise ValueError(f"Invalid {hook_type} hook playbook_base64: not valid base64 encoding. Error: {e}") from e
    except UnicodeDecodeError as e:
        raise ValueError(f"Invalid {hook_type} hook playbook_base64: not valid UTF-8 after decoding. Error: {e}") from e

    # Validate YAML syntax
    try:
        playbook_data = yaml.safe_load(playbook_text)
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid {hook_type} hook playbook_base64: not valid YAML syntax. Error: {e}") from e

    # Validate Ansible playbook structure (must be a non-empty list)
    if not isinstance(playbook_data, list) or not playbook_data:
        raise ValueError(
            f"Invalid {hook_type} hook playbook_base64: Ansible playbook must be a non-empty list of plays"
        )


def create_hook_for_plan(
    hook_config: dict[str, Any],
    hook_type: str,
    fixture_store: dict[str, Any],
    ocp_admin_client: "DynamicClient",
    target_namespace: str,
) -> tuple[str, str]:
    """
    Create a Hook CR based on plan configuration.

    Args:
        hook_config (dict[str, Any]): Hook configuration with either 'expected_result' OR 'playbook_base64' (mutually exclusive)
        hook_type (str): "pre" or "post" for logging
        fixture_store (dict[str, Any]): Fixture store for resource tracking
        ocp_admin_client (DynamicClient): OpenShift admin client
        target_namespace (str): Namespace to create hook in

    Returns:
        tuple[str, str]: Tuple of (hook_name, hook_namespace)

    Raises:
        TypeError: If hook_config is not a dict
        ValueError: If expected_result is invalid or playbook is invalid base64
    """
    # Validate configuration
    validate_hook_config(hook_config, hook_type)

    expected_result = hook_config.get("expected_result")
    custom_playbook = hook_config.get("playbook_base64")

    # Determine playbook based on mode
    if custom_playbook:
        # Custom playbook mode - validate base64, UTF-8, and YAML syntax
        validate_custom_playbook(custom_playbook, hook_type)
        playbook = custom_playbook
        LOGGER.info("Using custom %s hook playbook", hook_type)
    else:
        # Predefined playbook mode
        if expected_result not in ("succeed", "fail"):
            raise ValueError(
                f"Invalid {hook_type} hook 'expected_result': must be 'succeed' or 'fail', got: '{expected_result}'"
            )
        playbook = HOOK_PLAYBOOK_FAIL if expected_result == "fail" else HOOK_PLAYBOOK_SUCCESS
        LOGGER.info("Using predefined %s hook playbook for expected_result='%s'", hook_type, expected_result)

    # Create the Hook CR
    hook = create_and_store_resource(
        client=ocp_admin_client,
        fixture_store=fixture_store,
        resource=Hook,
        namespace=target_namespace,
        playbook=playbook,
    )

    return hook.name, hook.namespace


def validate_all_vms_same_step(failed_steps: dict[str, str | None]) -> str:
    """
    Validate that all VMs failed at the same step and return the common step.

    Args:
        failed_steps (dict[str, str | None]): Dictionary mapping VM names to their failed step names (or None if unknown)

    Returns:
        str: The common failed step name (e.g., "PreHook", "PostHook")

    Raises:
        AssertionError: If VMs failed at different steps or no failed step found for any VM
    """
    unique_steps = {step for step in failed_steps.values() if step is not None}

    if not unique_steps:
        failed_info = ", ".join(f"'{vm}': {step}" for vm, step in failed_steps.items())
        raise AssertionError(f"Could not determine failed step for any VM. Results: {failed_info}")

    if len(unique_steps) > 1:
        failed_info = ", ".join(f"'{vm}': {step}" for vm, step in failed_steps.items())
        raise AssertionError(f"VMs failed at different steps: {failed_info}")

    common_step = unique_steps.pop()
    LOGGER.info("All VMs failed at step '%s'", common_step)
    return common_step


def validate_expected_hook_failure(
    actual_failed_step: str,
    plan_config: dict[str, Any],
) -> None:
    """
    Validate the actual failed step matches expected (predefined mode only).

    For custom playbook mode (no expected_result set), this is a no-op.

    Args:
        actual_failed_step: The actual failed step from validate_all_vms_same_step()
        plan_config: Plan configuration dict with hook settings

    Raises:
        AssertionError: If actual step doesn't match expected (predefined mode)
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


def create_hook_if_configured(
    plan: dict[str, Any],
    hook_key: str,
    hook_type: str,
    fixture_store: dict[str, Any],
    ocp_admin_client: "DynamicClient",
    target_namespace: str,
) -> None:
    """Create hook if configured in plan and store references.

    Args:
        plan (dict[str, Any]): Plan configuration dict
        hook_key (str): Key in plan dict ("pre_hook" or "post_hook")
        hook_type (str): Hook type for create_hook_for_plan ("pre" or "post")
        fixture_store (dict[str, Any]): Fixture store for resource tracking
        ocp_admin_client (DynamicClient): OpenShift client
        target_namespace (str): Target namespace for hook creation
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
