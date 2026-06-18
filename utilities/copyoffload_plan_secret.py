"""Copy-offload plan secret polling helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ocp_resources.plan import Plan
from ocp_resources.secret import Secret
from simple_logger.logger import get_logger
from timeout_sampler import TimeoutExpiredError, TimeoutSampler

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

LOGGER = get_logger(__name__)

PLAN_SECRET_WAIT_TIMEOUT = 60
PLAN_NAME_LABEL = "plan-name"
POPULATOR_LABEL = "isPopulator"
COPY_OFFLOAD_PVC_NAME_TEMPLATE = "pvc"


def plan_uses_copyoffload(plan: Plan) -> bool:
    """Return whether the Plan CR uses copy-offload volume population.

    Args:
        plan (Plan): Plan resource to inspect.

    Returns:
        bool: True when the plan sets the copy-offload PVC naming template.
    """
    pvc_name_template = getattr(plan.instance.spec, "pvcNameTemplate", None)
    return pvc_name_template == COPY_OFFLOAD_PVC_NAME_TEMPLATE


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

    Call after the Migration CR is created. Forklift creates the plan populator secret
    when migration starts, not when the Plan reaches Ready.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        namespace (str): Namespace where the plan and secret exist.
        plan_name (str): Name of the Plan (secret will be named ``{plan_name}-*``).

    Raises:
        TimeoutError: If the secret is not created within PLAN_SECRET_WAIT_TIMEOUT seconds.
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
    except TimeoutExpiredError as err:
        secret_names = _list_namespace_secret_names(ocp_admin_client=ocp_admin_client, namespace=namespace)
        raise TimeoutError(
            f"Timeout waiting for plan secret '{plan_name}-*' in namespace '{namespace}' "
            f"after {PLAN_SECRET_WAIT_TIMEOUT}s (secrets present: {secret_names})"
        ) from err


def wait_for_copyoffload_plan_secret(
    ocp_admin_client: DynamicClient,
    plan: Plan,
    namespace: str,
) -> None:
    """Wait for the plan populator secret when the plan uses copy-offload.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        plan (Plan): Plan resource tied to the migration.
        namespace (str): Namespace where the migration and secret exist.

    Raises:
        TimeoutError: If the secret is not created within PLAN_SECRET_WAIT_TIMEOUT seconds.
    """
    if not plan_uses_copyoffload(plan=plan):
        return
    wait_for_plan_secret(
        ocp_admin_client=ocp_admin_client,
        namespace=namespace,
        plan_name=plan.name,
    )
