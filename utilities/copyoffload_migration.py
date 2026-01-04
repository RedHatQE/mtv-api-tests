"""
Copy-offload migration utilities for MTV tests.

This module provides copy-offload specific functionality for VM migration tests,
specifically credential management for copy-offload configurations.
"""

from __future__ import annotations

import os
from typing import Any

from kubernetes.dynamic import DynamicClient
from ocp_resources.secret import Secret
from simple_logger.logger import get_logger
from timeout_sampler import TimeoutExpiredError, TimeoutSampler

LOGGER = get_logger(__name__)


def get_copyoffload_credential(
    credential_name: str,
    copyoffload_config: dict[str, Any],
) -> str | None:
    """
    Get a copyoffload credential from environment variable or config file.

    Environment variables take precedence over config file values.

    Args:
        credential_name: Name of the credential (e.g., "storage_hostname", "ontap_svm")
        copyoffload_config: Copyoffload configuration dictionary

    Returns:
        str | None: Credential value from env var or config, or None if not found
    """
    env_var_name = f"COPYOFFLOAD_{credential_name.upper()}"
    return os.getenv(env_var_name) or copyoffload_config.get(credential_name)


def wait_for_plan_secret(ocp_admin_client: DynamicClient, namespace: str, plan_name: str) -> None:
    """
    Wait for Forklift to create plan-specific secret for copy-offload.

    When a Plan is created with copy-offload configuration, ForkliftController
    should automatically create a plan-specific secret containing storage credentials.
    This function polls for that secret's existence.

    Args:
        ocp_admin_client: OpenShift dynamic client
        namespace: Namespace where the plan and secret exist
        plan_name: Name of the Plan (secret will be named {plan_name}-*)

    Note:
        Times out after 60 seconds but continues anyway (logs warning).
        The migration will fail with clearer error if secret is missing.
    """
    LOGGER.info("Copy-offload: waiting for Forklift to create plan-specific secret...")
    try:
        for _ in TimeoutSampler(
            wait_timeout=60,
            sleep=2,
            func=lambda: any(
                s.name.startswith(f"{plan_name}-") for s in Secret.get(client=ocp_admin_client, namespace=namespace)
            ),
        ):
            break
    except TimeoutExpiredError:
        LOGGER.warning(f"Timeout waiting for plan secret '{plan_name}-*' - continuing anyway")
