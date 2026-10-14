from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ocp_resources.event import Event
from ocp_resources.persistent_volume_claim import PersistentVolumeClaim
from pytest_testconfig import config as py_config
from timeout_sampler import TimeoutExpiredError, TimeoutSampler

from exceptions.exceptions import StorageProvisioningError
from utilities.naming import generate_name_with_uuid
from utilities.resources import create_and_store_resource

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

LOGGER = logging.getLogger(__name__)

# Canary PVC size — small enough to bind within seconds on healthy storage.
_CANARY_SIZE = "1Gi"
# Overall canary observation window, reached only when the canary is Pending without
# ProvisioningFailed events (Pending alone is normal mid-provision: WaitForFirstConsumer,
# slow storage). Confirmed provisioning failure raises earlier, ~2-3 minutes after PVC
# creation, via the event path below.
_CANARY_OBSERVATION_WINDOW_SECONDS = 300
# Seconds between canary phase / event checks.
_PROVISIONING_FAILURE_POLL_INTERVAL_SECONDS = 30
# Consecutive polls that must observe a Warning ProvisioningFailed event before failing
# the run. A single event can be transient; the observed broken-Ceph timeline emits them
# continuously (first failure event ~2 minutes after PVC creation).
_PROVISIONING_FAILURE_CONFIRMATIONS = 3


def _get_provisioning_failure_message(
    client: DynamicClient,
    namespace: str,
    pvc_name: str,
) -> str | None:
    """Return the provisioner error from the PVC's most recent Warning ProvisioningFailed event.

    Args:
        client (DynamicClient): OpenShift client for the target cluster.
        namespace (str): Namespace where the canary PVC exists.
        pvc_name (str): Name of the canary PVC.

    Returns:
        str | None: The provisioner's error message, or None when no Warning
            ProvisioningFailed event exists for the PVC.
    """
    events = Event.list(
        client=client,
        namespace=namespace,
        field_selector=(
            f"involvedObject.kind=PersistentVolumeClaim,"
            f"involvedObject.name={pvc_name},"
            f"reason=ProvisioningFailed,type=Warning"
        ),
        since_seconds=_CANARY_OBSERVATION_WINDOW_SECONDS,
    )
    if not events:
        return None
    # Event.list sorts by lastTimestamp, most recent first.
    return str(events[0].get("message") or "unknown provisioner error")


def verify_destination_storage_provisioning(
    client: DynamicClient,
    fixture_store: dict[str, Any],
    namespace: str,
    session_uuid: str,
) -> None:
    """Fail fast when the run's storage class cannot provision volumes.

    Creates a small canary PVC in the target namespace using the run's configured
    storage class and watches it for Warning ``ProvisioningFailed`` events from the
    provisioner. Teardown of the canary PVC is registered by
    ``create_and_store_resource()`` and handled by session teardown.

    A Pending canary without failure events is NOT an error — binding can legitimately
    be slow (WaitForFirstConsumer, slow storage). Only consecutive, confirmed
    ``ProvisioningFailed`` observations fail the run.

    Args:
        client (DynamicClient): OpenShift client for the target cluster.
        fixture_store (dict[str, Any]): Fixture store — registers the canary PVC for teardown.
        namespace (str): Target namespace for the canary PVC.
        session_uuid (str): Session UUID, used for the canary PVC name.

    Raises:
        StorageProvisioningError: If the provisioner reports confirmed ProvisioningFailed
            events for the canary PVC.
    """
    storage_class = py_config["storage_class"]
    canary = create_and_store_resource(
        client=client,
        fixture_store=fixture_store,
        resource=PersistentVolumeClaim,
        name=generate_name_with_uuid(name=f"{session_uuid}-storage-canary"),
        namespace=namespace,
        storage_class=storage_class,
        access_modes=[PersistentVolumeClaim.AccessMode.RWO],
        size=_CANARY_SIZE,
    )
    LOGGER.info(
        f"Storage canary PVC '{canary.name}' created in namespace '{namespace}' "
        f"(storage class '{storage_class}', size {_CANARY_SIZE})"
    )

    failure_observations = 0
    try:
        for phase in TimeoutSampler(
            wait_timeout=_CANARY_OBSERVATION_WINDOW_SECONDS,
            sleep=_PROVISIONING_FAILURE_POLL_INTERVAL_SECONDS,
            func=lambda: canary.instance.get("status", {}).get("phase"),
        ):
            if phase == PersistentVolumeClaim.Status.BOUND:
                LOGGER.info(
                    f"Storage canary PVC '{canary.name}' bound — storage class '{storage_class}' can provision volumes"
                )
                return

            provisioner_message = _get_provisioning_failure_message(
                client=client,
                namespace=namespace,
                pvc_name=canary.name,
            )
            if provisioner_message is None:
                # Pending without ProvisioningFailed events is normal mid-provision.
                failure_observations = 0
                continue

            failure_observations += 1
            LOGGER.warning(
                f"Storage canary PVC '{canary.name}' ProvisioningFailed "
                f"({failure_observations}/{_PROVISIONING_FAILURE_CONFIRMATIONS}): {provisioner_message}"
            )
            if failure_observations >= _PROVISIONING_FAILURE_CONFIRMATIONS:
                raise StorageProvisioningError(
                    storage_class=storage_class,
                    pvc_name=canary.name,
                    provisioner_message=provisioner_message,
                )

    except TimeoutExpiredError:
        # Not a failure: Pending without ProvisioningFailed events means binding is
        # slow, not broken. Proceed and let migrations surface any real issue.
        LOGGER.warning(
            f"Storage canary PVC '{canary.name}' still Pending after "
            f"{_CANARY_OBSERVATION_WINDOW_SECONDS}s without ProvisioningFailed events — "
            "proceeding with the run"
        )
