from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

from ocp_resources.conversion import Conversion
from ocp_resources.pod import Pod
from ocp_resources.resource import NotFoundError
from ocp_resources.secret import Secret
from simple_logger.logger import get_logger
from timeout_sampler import TimeoutExpiredError, TimeoutSampler

from exceptions.exceptions import ConversionError
from utilities.resources import create_and_store_resource

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient
    from libs.base_provider import BaseProvider
    from libs.providers.vmware import VMWareProvider

LOGGER = get_logger(name=__name__)

CONVERSION_TERMINAL_PHASES = {Conversion.Status.SUCCEEDED, Conversion.Status.FAILED, Conversion.Condition.CANCELED}
# Not exposed by openshift-python-wrapper — mirrors forklift constants:
CONVERSION_TYPE_DEEP_INSPECTION = "DeepInspection"  # forklift: DeepInspection (conversion.go:20)
CONVERSION_STAGE_FINISHED = "Finished"  # forklift: StageFinished (conversion.go:55)
VDDK_IMAGE_NOT_SET_CONDITION = "VDDKImageNotSet"  # forklift: validation.go:14
DI_SNAPSHOT_NAME = "forklift-deep-inspection"  # forklift: snapshotName (client.go:21)
DI_SNAPSHOT_CREATION_TIMEOUT = 120
DI_POD_CREATION_TIMEOUT = 120
DI_POD_CLEANUP_TIMEOUT = 60
DI_SNAPSHOT_CLEANUP_TIMEOUT = 180


def create_di_connection_secret(
    client: "DynamicClient",
    fixture_store: dict[str, Any],
    source_provider: "BaseProvider",
    target_namespace: str,
) -> Secret:
    """Create a connection secret for standalone Deep Inspection.

    Replicates the forklift controller behavior (kubevirt.go:936-952):
    copies the source provider secret data and injects the provider URL
    and SSL fingerprint.

    Args:
        client (DynamicClient): OpenShift admin client.
        fixture_store (dict[str, Any]): Resource tracking dictionary.
        source_provider (BaseProvider): Source provider with ocp_resource.
        target_namespace (str): Namespace for the secret.

    Returns:
        Secret: The created connection secret.

    Raises:
        ValueError: If provider CR, secret, or fingerprint is missing or not found on cluster.
    """
    provider_cr = source_provider.ocp_resource
    if provider_cr is None:
        raise ValueError("source_provider.ocp_resource is not set")
    try:
        provider_instance = provider_cr.instance
    except NotFoundError as err:
        raise ValueError(f"Provider '{provider_cr.name}' not found on cluster") from err
    provider_spec = provider_instance.spec

    provider_secret_ref = provider_spec.secret
    if not provider_secret_ref:
        raise ValueError(f"Provider '{provider_cr.name}' has no secret reference in spec")

    source_secret = Secret(
        client=client,
        name=provider_secret_ref.name,
        namespace=provider_secret_ref.namespace,
    )
    try:
        secret_instance = source_secret.instance
    except NotFoundError as err:
        raise ValueError(
            f"Provider secret '{source_secret.name}' in namespace '{source_secret.namespace}' not found on cluster"
        ) from err
    raw_data = secret_instance.data
    if not raw_data:
        raise ValueError(f"Provider secret '{source_secret.name}' has no data")
    secret_data: dict[str, str] = dict(raw_data)

    provider_url = provider_spec.url
    if not provider_url:
        raise ValueError(f"Provider '{provider_cr.name}' has no URL in spec")
    secret_data["url"] = base64.b64encode(provider_url.encode()).decode()

    provider_status = provider_instance.status
    if not provider_status:
        raise ValueError(f"Provider '{provider_cr.name}' has no status — provider may not be reconciled")
    fingerprint = provider_status.get("fingerprint")
    if not fingerprint:
        raise ValueError(f"Provider '{provider_cr.name}' has no fingerprint in status")
    secret_data["fingerprint"] = base64.b64encode(fingerprint.encode()).decode()

    return create_and_store_resource(
        client=client,
        fixture_store=fixture_store,
        resource=Secret,
        namespace=target_namespace,
        data_dict=secret_data,
    )


def cancel_conversion(conversion: Conversion) -> None:
    """Cancel a Conversion CR by patching the status subresource.

    Mirrors the forklift controller's CancelConversion() pattern
    (kubevirt.go:973-975): sets phase=Canceled, stage=Finished via
    a status subresource merge-patch. The caller is responsible for
    ensuring the conversion is in a cancelable state (e.g., Running).

    Args:
        conversion (Conversion): The Conversion CR to cancel.
    """
    api_resource = conversion.client.resources.get(api_version=conversion.api_version, kind=conversion.kind)
    api_resource.status.patch(
        body={"status": {"phase": Conversion.Condition.CANCELED, "stage": CONVERSION_STAGE_FINISHED}},
        name=conversion.name,
        namespace=conversion.namespace,
        content_type="application/merge-patch+json",
    )
    LOGGER.info(f"Canceled conversion '{conversion.name}'")


def wait_for_conversion_phase(conversion: Conversion, phase: str, timeout: int) -> None:
    """Wait for a Conversion CR to reach a specific phase.

    Polls the Conversion CR status until the phase matches the expected
    value. Useful for timing cancel operations (wait for Running before
    canceling). Raises ConversionError if the conversion reaches a
    different terminal phase before the target.

    Args:
        conversion (Conversion): The Conversion CR to monitor.
        phase (str): Target phase to wait for (e.g., Conversion.Status.RUNNING).
        timeout (int): Maximum wait time in seconds.

    Raises:
        ConversionError: If the conversion reaches a terminal phase other
            than the target, or if the timeout expires.
    """
    last_phase = ""

    try:
        for sample in TimeoutSampler(
            wait_timeout=timeout,
            sleep=3,
            func=lambda: conversion.instance.status,
        ):
            if not sample:
                continue

            current_phase = sample.get("phase", "")
            last_phase = current_phase
            if current_phase == phase:
                LOGGER.info(f"Conversion '{conversion.name}' reached phase '{phase}'")
                return

            if current_phase in CONVERSION_TERMINAL_PHASES:
                raise ConversionError(
                    conversion_name=conversion.name,
                    phase=current_phase,
                    message=f"Reached terminal phase '{current_phase}' before target phase '{phase}'",
                )

    except TimeoutExpiredError as err:
        raise ConversionError(
            conversion_name=conversion.name,
            phase=last_phase or "Unknown",
            message=f"Did not reach phase '{phase}' within {timeout}s",
        ) from err


def wait_for_di_snapshot(
    source_provider: "VMWareProvider",
    vm_name: str,
    timeout: int = DI_SNAPSHOT_CREATION_TIMEOUT,
) -> None:
    """Wait for the DI snapshot to appear on the source VM.

    Polls the source provider until the forklift-deep-inspection snapshot
    exists. Used to ensure the DI pipeline has fully started before
    performing cancel operations.

    Args:
        source_provider (VMWareProvider): Source provider with list_snapshots method.
        vm_name (str): Source VM name to check snapshots on.
        timeout (int): Maximum wait time in seconds.

    Raises:
        ValueError: If the source VM is not found in the provider.
        TimeoutExpiredError: If snapshot does not appear within timeout.
    """
    vm = source_provider.get_vm_by_name(query=vm_name)
    try:
        for snapshots in TimeoutSampler(
            wait_timeout=timeout,
            sleep=5,
            func=lambda: [s for s in source_provider.list_snapshots(vm) if s.name == DI_SNAPSHOT_NAME],
        ):
            if snapshots:
                LOGGER.info(f"DI snapshot '{DI_SNAPSHOT_NAME}' found on VM '{vm_name}'")
                return
    except TimeoutExpiredError as err:
        raise TimeoutExpiredError(
            f"DI snapshot '{DI_SNAPSHOT_NAME}' not found on VM '{vm_name}' within {timeout}s"
        ) from err


def create_conversion_resource(
    client: "DynamicClient",
    fixture_store: dict[str, Any],
    connection_secret: Secret,
    vm_id: str,
    vm_name: str,
    target_namespace: str,
    vddk_image: str | None = None,
    snapshot_moref: str | None = None,
) -> Conversion:
    """Create a standalone DeepInspection Conversion CR.

    Args:
        client (DynamicClient): OpenShift admin client.
        fixture_store (dict[str, Any]): Resource tracking dictionary.
        connection_secret (Secret): Connection secret for vSphere access.
        vm_id (str): Source VM ID from inventory.
        vm_name (str): Source VM name.
        target_namespace (str): Namespace for the Conversion CR and pods.
        vddk_image (str | None): VDDK init container image. Required for
            DeepInspection — omit to test validation error (VDDKImageNotSet).
        snapshot_moref (str | None): vSphere snapshot MOREF. If provided, snapshot
            creation stages are skipped.

    Returns:
        Conversion: The created Conversion CR.
    """
    settings: dict[str, str] = {}
    if snapshot_moref:
        settings["SNAPSHOT_MOREF"] = snapshot_moref

    kwargs: dict[str, Any] = {
        "client": client,
        "fixture_store": fixture_store,
        "resource": Conversion,
        "namespace": target_namespace,
        "type": CONVERSION_TYPE_DEEP_INSPECTION,
        "connection": {"secret": {"name": connection_secret.name, "namespace": connection_secret.namespace}},
        "vm": {"id": vm_id, "name": vm_name, "type": "VirtualMachine"},
        "target_namespace": target_namespace,
    }
    if vddk_image is not None:
        kwargs["vddk_image"] = vddk_image
    if settings:
        kwargs["settings"] = settings

    return create_and_store_resource(**kwargs)


def wait_for_conversion_complete(conversion: Conversion, timeout: int) -> None:
    """Wait for a Conversion CR to reach a terminal phase.

    Polls the Conversion CR status until phase reaches Succeeded, Failed,
    or Canceled. Logs phase and stage transitions.

    Args:
        conversion (Conversion): The Conversion CR to monitor.
        timeout (int): Maximum wait time in seconds.

    Raises:
        ConversionError: If the conversion reaches Failed or Canceled phase,
            or if the timeout expires.
    """
    last_phase = ""
    last_stage = ""

    try:
        for sample in TimeoutSampler(
            wait_timeout=timeout,
            sleep=5,
            func=lambda: conversion.instance.status,
        ):
            if not sample:
                continue

            phase = sample.get("phase", "")
            stage = sample.get("stage", "")

            if phase != last_phase or stage != last_stage:
                LOGGER.info(f"Conversion '{conversion.name}' phase='{phase}' stage='{stage}'")
                last_phase = phase
                last_stage = stage

            if phase in CONVERSION_TERMINAL_PHASES:
                if phase != Conversion.Status.SUCCEEDED:
                    conditions = sample.get("conditions", [])
                    messages = [
                        c.get("message", "")
                        for c in conditions
                        if c.get("category") == "Critical" and c.get("status") == "True"
                    ]
                    raise ConversionError(
                        conversion_name=conversion.name,
                        phase=phase,
                        message="; ".join(messages),
                    )
                return

    except TimeoutExpiredError as err:
        raise ConversionError(
            conversion_name=conversion.name,
            phase=last_phase or "Unknown",
            message=f"Timed out after {timeout}s at stage '{last_stage}'",
        ) from err


def verify_di_results(conversion: Conversion, vm_name: str) -> None:
    """Verify Deep Inspection results on a completed Conversion CR.

    Checks that the inspection produced valid results including OS info
    and filesystem data. Uses the source VM name for error context.

    Args:
        conversion (Conversion): A Succeeded Conversion CR.
        vm_name (str): Source VM name for error context.

    Raises:
        AssertionError: If results are missing or incomplete.
    """
    status = conversion.instance.status
    assert status, f"VM '{vm_name}': Conversion '{conversion.name}' has no status"

    results = status.get("inspectionResult")
    assert results, f"VM '{vm_name}': Conversion '{conversion.name}' has no inspectionResult in status"

    os_info = results.get("osInfo")
    assert os_info and os_info.get("name"), (
        f"VM '{vm_name}': Conversion '{conversion.name}' inspectionResult missing osInfo or osInfo.name"
    )

    filesystems = results.get("filesystems")
    assert filesystems, f"VM '{vm_name}': Conversion '{conversion.name}' inspectionResult missing filesystems"

    passed = results.get("allChecksPassed")
    assert isinstance(passed, bool), (
        f"VM '{vm_name}': Conversion '{conversion.name}' allChecksPassed is not a boolean: {passed}"
    )

    LOGGER.info(
        f"DI results for VM '{vm_name}': allChecksPassed={passed}, "
        f"os={os_info.get('name')} {os_info.get('version', '')}, "
        f"filesystems={len(filesystems)}"
    )


def wait_for_critical_conditions(
    conversion: Conversion,
    timeout: int,
) -> tuple[list[Any], str]:
    """Wait for Critical conditions to appear on a Conversion CR.

    Polls the Conversion CR status until at least one condition with
    category=Critical and status=True appears. Returns the critical
    conditions and the phase at the time they were found.

    Args:
        conversion (Conversion): The Conversion CR to monitor.
        timeout (int): Maximum wait time in seconds.

    Returns:
        tuple[list[Any], str]: (critical_conditions, phase) at the time
            critical conditions were detected.

    Raises:
        AssertionError: If no Critical conditions appear within timeout.
    """
    conditions: list[Any] = []
    phase = ""
    critical: list[Any] = []

    try:
        for sample in TimeoutSampler(
            wait_timeout=timeout,
            sleep=3,
            func=lambda: conversion.instance.status,
        ):
            if not sample:
                continue
            conditions = sample.get("conditions", [])
            phase = sample.get("phase", "")
            critical = [c for c in conditions if c.get("category") == "Critical" and c.get("status") == "True"]
            if critical:
                LOGGER.info(f"Conversion '{conversion.name}' has {len(critical)} Critical condition(s)")
                break
    except TimeoutExpiredError as err:
        last_condition_types = [c.get("type") for c in conditions]
        raise AssertionError(
            f"Conversion '{conversion.name}' expected Critical conditions within {timeout}s. "
            f"Last phase='{phase}', conditions={last_condition_types}"
        ) from err

    return critical, phase


def wait_for_conversion_pods(
    conversion: Conversion,
    timeout: int = DI_POD_CREATION_TIMEOUT,
) -> list["Pod"]:
    """Wait for at least one pod associated with a Conversion CR to appear.

    Polls for pods matching the conversion label selector until at least
    one exists. The forklift controller creates the snapshot before the
    pod, so the pod may not exist yet when the snapshot is found.

    Also checks for terminal phases on each iteration — if the conversion
    fails or is canceled before creating a pod, raises immediately instead
    of waiting until timeout.

    Args:
        conversion (Conversion): The Conversion CR whose pods to wait for.
        timeout (int): Maximum wait time in seconds.

    Returns:
        list[Pod]: The conversion pods found.

    Raises:
        ConversionError: If the conversion reaches a terminal phase before pods appear,
            or if the Conversion CR is deleted during polling.
        AssertionError: If no pods appear within timeout.
    """

    def _poll() -> list["Pod"]:
        try:
            status = conversion.instance.status or {}
        except NotFoundError as err:
            raise ConversionError(
                conversion_name=conversion.name,
                phase="Unknown",
                message="Conversion not found while waiting for pods",
            ) from err
        phase = status.get("phase", "")
        if phase in CONVERSION_TERMINAL_PHASES:
            raise ConversionError(
                conversion_name=conversion.name,
                phase=phase,
                message=f"Reached terminal phase '{phase}' before pods were created",
            )
        return list(
            Pod.get(
                client=conversion.client,
                namespace=conversion.namespace,
                label_selector=f"conversion={conversion.name}",
            )
        )

    try:
        for pods in TimeoutSampler(
            wait_timeout=timeout,
            sleep=3,
            func=_poll,
        ):
            if pods:
                LOGGER.info(f"Found {len(pods)} pod(s) for conversion '{conversion.name}'")
                return pods
    except TimeoutExpiredError as err:
        raise AssertionError(f"No conversion pods found for '{conversion.name}' within {timeout}s") from err

    return []


def wait_for_conversion_pods_cleanup(
    conversion: Conversion,
    timeout: int = DI_POD_CLEANUP_TIMEOUT,
) -> None:
    """Wait for all pods associated with a Conversion CR to be cleaned up.

    Polls for pods matching the conversion label selector until none remain.

    Args:
        conversion (Conversion): The Conversion CR whose pods to monitor.
        timeout (int): Maximum wait time in seconds.

    Raises:
        AssertionError: If pods are still present after timeout.
    """
    try:
        for pods in TimeoutSampler(
            wait_timeout=timeout,
            sleep=5,
            func=lambda: list(
                Pod.get(
                    client=conversion.client,
                    namespace=conversion.namespace,
                    label_selector=f"conversion={conversion.name}",
                )
            ),
        ):
            if not pods:
                LOGGER.info(f"All pods cleaned up for conversion '{conversion.name}'")
                return
    except TimeoutExpiredError as err:
        remaining = list(
            Pod.get(
                client=conversion.client,
                namespace=conversion.namespace,
                label_selector=f"conversion={conversion.name}",
            )
        )
        raise AssertionError(
            f"Conversion pods still present {timeout}s after cancel: {[p.name for p in remaining]}"
        ) from err


def wait_for_di_snapshot_cleanup(
    source_provider: "VMWareProvider",
    vm_name: str,
    timeout: int = DI_SNAPSHOT_CLEANUP_TIMEOUT,
) -> None:
    """Wait for the DI snapshot to be removed from the source VM.

    Polls the source provider until the forklift-deep-inspection snapshot
    no longer exists on the VM.

    Args:
        source_provider (VMWareProvider): Source provider with list_snapshots method.
        vm_name (str): Source VM name to check snapshots on.
        timeout (int): Maximum wait time in seconds.

    Raises:
        AssertionError: If DI snapshot is still present after timeout.
    """
    vm = source_provider.get_vm_by_name(query=vm_name)
    try:
        for snapshots in TimeoutSampler(
            wait_timeout=timeout,
            sleep=10,
            func=lambda: [s for s in source_provider.list_snapshots(vm) if s.name == DI_SNAPSHOT_NAME],
        ):
            if not snapshots:
                LOGGER.info(f"DI snapshot '{DI_SNAPSHOT_NAME}' cleaned up from VM '{vm_name}'")
                return
    except TimeoutExpiredError as err:
        remaining = [s.name for s in source_provider.list_snapshots(vm)]
        raise AssertionError(
            f"DI snapshot '{DI_SNAPSHOT_NAME}' still present on VM '{vm_name}' "
            f"{timeout}s after cancel. All snapshots: {remaining}"
        ) from err
