from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ocp_resources.persistent_volume_claim import PersistentVolumeClaim
from ocp_resources.storage_class import StorageClass
from pytest_testconfig import config as py_config
from timeout_sampler import TimeoutExpiredError, TimeoutSampler

from exceptions.exceptions import StorageProvisioningError
from utilities.naming import generate_name_with_uuid
from utilities.resources import create_and_store_resource

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

LOGGER = logging.getLogger(__name__)

# Kubernetes default when a StorageClass omits volumeBindingMode.
_IMMEDIATE_BINDING_MODE = "Immediate"
# WaitForFirstConsumer: binding is deferred until a pod consumes the PVC, so a bare
# canary PVC can never bind and says nothing about provisioning health — skipped.
_WAIT_FOR_FIRST_CONSUMER_BINDING_MODE = "WaitForFirstConsumer"
# Canary PVC size — small enough to bind within seconds on healthy storage.
_CANARY_SIZE = "1Gi"
# How long the canary may stay Pending before provisioning is declared broken.
# Healthy Immediate storage binds in seconds; the window only exists so legitimately
# slow storage is not misreported as broken.
_CANARY_BIND_TIMEOUT_SECONDS = 300
# Seconds between canary phase checks.
_CANARY_BIND_POLL_INTERVAL_SECONDS = 30


def verify_destination_storage_provisioning(
    client: DynamicClient,
    fixture_store: dict[str, Any],
    namespace: str,
    session_uuid: str,
) -> None:
    """Fail fast when the run's storage class cannot provision volumes.

    Creates a small canary PVC in the target namespace using the run's configured
    storage class and waits for it to bind. Teardown of the canary PVC is registered
    by ``create_and_store_resource()`` and handled by session teardown.

    Storage classes with ``volumeBindingMode: WaitForFirstConsumer`` are skipped:
    binding there is deferred until a pod consumes the PVC, so a bare canary can
    never bind and carries no signal.

    Args:
        client (DynamicClient): OpenShift client for the target cluster.
        fixture_store (dict[str, Any]): Fixture store — registers the canary PVC for teardown.
        namespace (str): Target namespace for the canary PVC.
        session_uuid (str): Session UUID, used for the canary PVC name.

    Raises:
        ValueError: If the configured storage class does not exist on the cluster.
        StorageProvisioningError: If the canary PVC is still Pending after
            ``_CANARY_BIND_TIMEOUT_SECONDS``.
    """
    storage_class_name = py_config["storage_class"]
    storage_class = StorageClass(client=client, name=storage_class_name)
    if not storage_class.exists:
        raise ValueError(f"Storage class '{storage_class_name}' not found on the cluster")

    binding_mode = storage_class.instance.get("volumeBindingMode") or _IMMEDIATE_BINDING_MODE
    if binding_mode == _WAIT_FOR_FIRST_CONSUMER_BINDING_MODE:
        LOGGER.info(
            f"Storage class '{storage_class_name}' uses WaitForFirstConsumer binding — "
            "storage canary skipped (a bare canary PVC cannot bind without a consumer)"
        )
        return

    canary = create_and_store_resource(
        client=client,
        fixture_store=fixture_store,
        resource=PersistentVolumeClaim,
        name=generate_name_with_uuid(name=f"{session_uuid}-storage-canary"),
        namespace=namespace,
        storage_class=storage_class_name,
        access_modes=[PersistentVolumeClaim.AccessMode.RWO],
        size=_CANARY_SIZE,
    )
    LOGGER.info(
        f"Storage canary PVC '{canary.name}' created in namespace '{namespace}' "
        f"(storage class '{storage_class_name}', size {_CANARY_SIZE})"
    )

    try:
        for phase in TimeoutSampler(
            wait_timeout=_CANARY_BIND_TIMEOUT_SECONDS,
            sleep=_CANARY_BIND_POLL_INTERVAL_SECONDS,
            func=lambda: canary.instance.get("status", {}).get("phase"),
        ):
            if phase == PersistentVolumeClaim.Status.BOUND:
                LOGGER.info(
                    f"Storage canary PVC '{canary.name}' bound — storage class "
                    f"'{storage_class_name}' can provision volumes"
                )
                return

    except TimeoutExpiredError:
        raise StorageProvisioningError(
            storage_class=storage_class_name,
            pvc_name=canary.name,
            detail=(f"still Pending after {_CANARY_BIND_TIMEOUT_SECONDS}s (volumeBindingMode={binding_mode})"),
        ) from None
