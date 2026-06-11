"""Copy-offload plan secret polling helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ocp_resources.secret import Secret
from simple_logger.logger import get_logger
from timeout_sampler import TimeoutExpiredError, TimeoutSampler

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

LOGGER = get_logger(__name__)


def wait_for_plan_secret(ocp_admin_client: DynamicClient, namespace: str, plan_name: str) -> None:
    """Wait for Forklift to create the plan-specific secret for copy-offload.

    When a Plan is created with copy-offload configuration, ForkliftController
    should automatically create a plan-specific secret containing storage credentials.
    This function polls for that secret's existence.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        namespace (str): Namespace where the plan and secret exist.
        plan_name (str): Name of the Plan (secret will be named ``{plan_name}-*``).

    Note:
        Times out after 60 seconds but continues anyway (logs warning).
        The migration will fail with a clearer error if the secret is missing.
    """
    LOGGER.info("Copy-offload: waiting for Forklift to create plan-specific secret...")
    try:
        for sample in TimeoutSampler(
            wait_timeout=60,
            sleep=2,
            func=lambda: any(
                s.name.startswith(f"{plan_name}-") for s in Secret.get(client=ocp_admin_client, namespace=namespace)
            ),
        ):
            if sample:
                return
    except TimeoutExpiredError:
        LOGGER.warning(f"Timeout waiting for plan secret '{plan_name}-*' - continuing anyway")
