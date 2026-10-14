from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ocp_resources.persistent_volume_claim import PersistentVolumeClaim
from ocp_resources.storage_class import StorageClass
from pytest_testconfig import config as py_config
from timeout_sampler import TimeoutExpiredError, TimeoutSampler

from exceptions.exceptions import StorageProvisioningError
from utilities.naming import generate_name_with_uuid

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


def _wait_for_canary_bound(
    canary: PersistentVolumeClaim,
    storage_class_name: str,
    binding_mode: str,
) -> None:
    """Wait for the canary PVC to bind.

    Args:
        canary (PersistentVolumeClaim): The canary PVC to monitor.
        storage_class_name (str): Storage class the canary was created with.
        binding_mode (str): The storage class volumeBindingMode (for the error message).

    Raises:
        StorageProvisioningError: If the canary is still Pending after
            ``_CANARY_BIND_TIMEOUT_SECONDS``.
    """
    try:
        for phase in TimeoutSampler(
            wait_timeout=_CANARY_BIND_TIMEOUT_SECONDS,
            sleep=_CANARY_BIND_POLL_INTERVAL_SECONDS,
            func=lambda: canary.instance.get("status", {}).get("phase"),
            # Empty dict: any status-lookup error (API/authorization failure) re-raises
            # immediately instead of being retried into the bind timeout and reported
            # as a Pending PVC.
            exceptions_dict={},
        ):
            if phase == PersistentVolumeClaim.Status.BOUND:
                LOGGER.info(
                    f"Storage canary PVC '{canary.name}' bound — storage class "
                    f"'{storage_class_name}' can provision volumes"
                )
                return

    except TimeoutExpiredError as exc:
        if exc.last_exp is not None:
            # The sampler re-raised an underlying lookup error immediately.
            raise StorageProvisioningError(
                storage_class=storage_class_name,
                pvc_name=canary.name,
                detail=f"canary status lookup failed: {exc.last_exp}",
            ) from exc.last_exp
        raise StorageProvisioningError(
            storage_class=storage_class_name,
            pvc_name=canary.name,
            detail=(f"still Pending after {_CANARY_BIND_TIMEOUT_SECONDS}s (volumeBindingMode={binding_mode})"),
        ) from None


def verify_destination_storage_provisioning(
    client: DynamicClient,
    namespace: str,
    session_uuid: str,
) -> None:
    """Fail fast when the run's storage class cannot provision volumes.

    Creates a small canary PVC in the target namespace using the run's configured
    storage class and waits for it to bind. The canary is created and torn down by
    the PersistentVolumeClaim context manager — it is not a test resource and never
    reaches session teardown (its name contains the session UUID, which the teardown
    leftover check matches).

    Storage classes with ``volumeBindingMode: WaitForFirstConsumer`` are skipped:
    binding there is deferred until a pod consumes the PVC, so a bare canary can
    never bind and carries no signal.

    Args:
        client (DynamicClient): OpenShift client for the target cluster.
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

    with PersistentVolumeClaim(
        client=client,
        name=generate_name_with_uuid(name=f"{session_uuid}-storage-canary"),
        namespace=namespace,
        storage_class=storage_class_name,
        accessmodes=PersistentVolumeClaim.AccessMode.RWO,
        size=_CANARY_SIZE,
    ) as canary:
        LOGGER.info(
            f"Storage canary PVC '{canary.name}' created in namespace '{namespace}' "
            f"(storage class '{storage_class_name}', size {_CANARY_SIZE})"
        )
        _wait_for_canary_bound(canary=canary, storage_class_name=storage_class_name, binding_mode=binding_mode)
