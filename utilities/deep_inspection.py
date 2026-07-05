from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

from ocp_resources.conversion import Conversion
from ocp_resources.resource import NotFoundError
from ocp_resources.secret import Secret
from simple_logger.logger import get_logger
from timeout_sampler import TimeoutExpiredError, TimeoutSampler

from exceptions.exceptions import ConversionError
from utilities.resources import create_and_store_resource

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient
    from libs.base_provider import BaseProvider

LOGGER = get_logger(name=__name__)

CONVERSION_TERMINAL_PHASES = {Conversion.Status.SUCCEEDED, Conversion.Status.FAILED, Conversion.Condition.CANCELED}


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


def create_conversion_resource(
    client: "DynamicClient",
    fixture_store: dict[str, Any],
    connection_secret: Secret,
    vm_id: str,
    vm_name: str,
    vddk_image: str,
    target_namespace: str,
    snapshot_moref: str | None = None,
) -> Conversion:
    """Create a standalone DeepInspection Conversion CR.

    Args:
        client (DynamicClient): OpenShift admin client.
        fixture_store (dict[str, Any]): Resource tracking dictionary.
        connection_secret (Secret): Connection secret for vSphere access.
        vm_id (str): Source VM ID from inventory.
        vm_name (str): Source VM name.
        vddk_image (str): VDDK init container image (required for DeepInspection).
        target_namespace (str): Namespace for the Conversion CR and pods.
        snapshot_moref (str | None): vSphere snapshot MOREF. If provided, snapshot
            creation stages are skipped.

    Returns:
        Conversion: The created Conversion CR.
    """
    settings: dict[str, str] = {}
    if snapshot_moref:
        settings["SNAPSHOT_MOREF"] = snapshot_moref

    return create_and_store_resource(
        client=client,
        fixture_store=fixture_store,
        resource=Conversion,
        namespace=target_namespace,
        type="DeepInspection",
        connection={"secret": {"name": connection_secret.name, "namespace": connection_secret.namespace}},
        vm={"id": vm_id, "name": vm_name, "type": "VirtualMachine"},
        vddk_image=vddk_image,
        target_namespace=target_namespace,
        settings=settings or None,
    )


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
                    messages = [c.get("message", "") for c in conditions if c.get("type") == "Critical"]
                    raise ConversionError(
                        conversion_name=conversion.name,
                        phase=phase,
                        message="; ".join(messages),
                    )
                return

    except TimeoutExpiredError:
        raise ConversionError(
            conversion_name=conversion.name,
            phase=last_phase or "Unknown",
            message=f"Timed out after {timeout}s at stage '{last_stage}'",
        )


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
