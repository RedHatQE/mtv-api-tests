"""Fixtures for isolated Plan archive cleanup checks."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from ocp_resources.namespace import Namespace

from utilities.naming import generate_name_with_uuid
from utilities.resources import create_and_store_resource

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient


@pytest.fixture(scope="class")
def plan_archive_vm_namespace(
    prepared_plan: dict[str, Any],
    fixture_store: dict[str, Any],
    ocp_admin_client: DynamicClient,
    session_uuid: str,
) -> str:
    """Create a class-dedicated VM destination namespace tracked for session cleanup.

    Args:
        prepared_plan: Prepared migration plan to update for VM cleanup.
        fixture_store: Session resource teardown store.
        ocp_admin_client: OpenShift admin client.
        session_uuid: Session-unique naming prefix.

    Returns:
        str: Dedicated VM destination namespace name.
    """
    namespace = create_and_store_resource(
        client=ocp_admin_client,
        fixture_store=fixture_store,
        resource=Namespace,
        name=generate_name_with_uuid(name=f"{session_uuid}-archive-vms"),
        label={
            "pod-security.kubernetes.io/enforce": "restricted",
            "pod-security.kubernetes.io/enforce-version": "latest",
            "mutatevirtualmachines.kubemacpool.io": "ignore",
        },
    )
    namespace.wait_for_status(status=namespace.Status.ACTIVE)
    prepared_plan["_vm_target_namespace"] = namespace.name
    return namespace.name
