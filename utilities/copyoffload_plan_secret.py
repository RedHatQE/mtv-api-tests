"""Copy-offload plan secret polling helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ocp_resources.secret import Secret
from simple_logger.logger import get_logger
from timeout_sampler import TimeoutExpiredError, TimeoutSampler

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

LOGGER = get_logger(__name__)

PLAN_SECRET_WAIT_TIMEOUT = 60
PLAN_NAME_LABEL = "plan-name"
POPULATOR_LABEL = "isPopulator"


def _plan_secret_exists(
    ocp_admin_client: DynamicClient,
    namespace: str,
    plan_name: str,
) -> bool:
    """Return whether Forklift created a plan-specific copy-offload secret.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        namespace (str): Namespace where secrets are listed.
        plan_name (str): Name of the Plan CR.

    Returns:
        bool: True if a matching plan secret exists.
    """
    for secret in Secret.get(client=ocp_admin_client, namespace=namespace):
        labels: dict[str, str] = secret.instance.metadata.labels or {}
        if labels.get(PLAN_NAME_LABEL) == plan_name and labels.get(POPULATOR_LABEL):
            return True
        if secret.name.startswith(f"{plan_name}-"):
            return True
    return False


def _list_namespace_secret_names(ocp_admin_client: DynamicClient, namespace: str) -> list[str]:
    """List secret names in a namespace for timeout diagnostics.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        namespace (str): Namespace where secrets are listed.

    Returns:
        list[str]: Secret names present in the namespace.
    """
    return [secret.name for secret in Secret.get(client=ocp_admin_client, namespace=namespace)]


def wait_for_plan_secret(ocp_admin_client: DynamicClient, namespace: str, plan_name: str) -> None:
    """Wait for Forklift to create the plan-specific secret for copy-offload.

    When a Plan is created with copy-offload configuration, ForkliftController
    may create a plan-specific secret containing storage credentials.
    This function polls for that secret's existence.

    MTV-5799 tracks moving this wait to migration start. Until then, a timeout at Plan
    Ready is expected because Forklift creates populator secrets when migration starts,
    not when the Plan becomes Ready. That is an intentional exception to "No Silent
    Recovery" so migration produces the actionable failure instead.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        namespace (str): Namespace where the plan and secret exist.
        plan_name (str): Name of the Plan (secret will be named ``{plan_name}-*``).
    """
    LOGGER.info("Copy-offload: waiting for Forklift to create plan-specific secret...")
    try:
        for sample in TimeoutSampler(
            wait_timeout=PLAN_SECRET_WAIT_TIMEOUT,
            sleep=2,
            func=lambda: _plan_secret_exists(
                ocp_admin_client=ocp_admin_client,
                namespace=namespace,
                plan_name=plan_name,
            ),
        ):
            if sample:
                return
    except TimeoutExpiredError:
        secret_names = _list_namespace_secret_names(ocp_admin_client=ocp_admin_client, namespace=namespace)
        # MTV-5799: remove this continue-on-timeout once wait_for_plan_secret runs at migration start.
        LOGGER.warning(
            f"Timeout waiting for plan secret '{plan_name}-*' in namespace '{namespace}' "
            f"after {PLAN_SECRET_WAIT_TIMEOUT}s (secrets present: {secret_names}) - continuing anyway"
        )
