"""
Fixtures for AAP (Ansible Automation Platform) hook integration tests.

Session-scoped fixtures deploy AWX on the cluster and configure MTV
to use it. Class-scoped fixtures create AAP Hook CRs for each test class.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Generator

import pytest
from ocp_resources.forklift_controller import ForkliftController
from ocp_resources.resource import ResourceEditor
from simple_logger.logger import get_logger

from utilities.aap import (
    AAP_TEST_PLAYBOOKS_REPO,
    AWX_ADMIN_USERNAME,
    AWX_NAMESPACE,
    AWX_POSTHOOK_PLAYBOOK,
    AWX_POSTHOOK_TEMPLATE_NAME,
    AWX_PREHOOK_PLAYBOOK,
    AWX_PREHOOK_TEMPLATE_NAME,
    AWX_PROJECT_NAME,
    awx_lifecycle_lock,
    clear_awx_install_owner,
    create_aap_token_secret,
    create_awx_auth_token,
    create_awx_instance,
    create_awx_inventory,
    create_awx_job_template,
    create_awx_project,
    deploy_awx_via_helm,
    get_awx_admin_password,
    get_awx_route_url,
    mark_awx_install_owner,
    register_awx_worker,
    teardown_awx,
    unregister_awx_worker,
    wait_for_awx_api_ready,
    wait_for_awx_project_sync,
    wait_for_awx_ready,
)
from utilities.hooks import create_hook_for_plan
from utilities.pytest_utils import is_dry_run
from utilities.utils import get_cluster_client

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

LOGGER = get_logger(__name__)

FORKLIFT_CONTROLLER_NAME: str = (
    "forklift-controller"  # Well-known name of the ForkliftController CR created by MTV operator
)
_AWX_CONTROLLER_REGISTERED_ATTR = "_mtv_awx_controller_registered"


def _is_xdist_worker(config: pytest.Config) -> bool:
    """Return True when this pytest process is an xdist worker.

    Args:
        config (pytest.Config): Pytest config for this process.

    Returns:
        bool: True for xdist workers, False for the controller or a non-xdist run.
    """
    return getattr(config, "workerinput", None) is not None


def _session_includes_hooks_tests(items: list[pytest.Item]) -> bool:
    """Return True when collected items include tests under tests/hooks.

    Args:
        items (list[pytest.Item]): Collected pytest items.

    Returns:
        bool: True if at least one item lives under the hooks test directory.
    """
    return any("hooks" in Path(str(getattr(item, "path", item.fspath))).parts for item in items)


def pytest_collection_modifyitems(session: pytest.Session, config: pytest.Config, items: list[pytest.Item]) -> None:
    """Register the controller process as an AWX session participant.

    xdist workers request ``awx_deployment`` lazily. A controller lease opened
    during collection stays alive until ``pytest_sessionfinish``, so a worker
    that finishes hooks tests cannot treat an empty lease set as "no future
    fixture users."
    """
    if _is_xdist_worker(config) or is_dry_run(config) or not _session_includes_hooks_tests(items):
        return
    client = get_cluster_client()
    with awx_lifecycle_lock(client=client):
        register_awx_worker(client=client)
    setattr(config, _AWX_CONTROLLER_REGISTERED_ATTR, True)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Tear down AWX after the full pytest session when this run owns it."""
    if not getattr(session.config, _AWX_CONTROLLER_REGISTERED_ATTR, False):
        return
    client = get_cluster_client()
    with awx_lifecycle_lock(client=client):
        should_teardown = unregister_awx_worker(client=client)
        if not should_teardown or session.config.getoption("skip_teardown"):
            return
        if teardown_awx():
            clear_awx_install_owner(client=client)


@pytest.fixture(scope="session")
def awx_deployment(
    ocp_admin_client: "DynamicClient",
) -> Generator[str, None, None]:
    """Deploy AWX via Helm and return the route URL.

    Installs the AWX Operator, creates an AWX instance with CephFS storage,
    and waits for all pods to be ready. AWX is a third-party CRD installed
    at runtime — cleanup is handled by namespace deletion in teardown_awx().

    Helm install is serialized across pytest-xdist workers with a file lock
    and PID leases. The pytest controller registers before tests start and
    tears AWX down in ``pytest_sessionfinish`` after every worker finishes.

    Args:
        ocp_admin_client: OpenShift admin client.

    Yields:
        str: AWX web route URL (https://...).

    Raises:
        TimeoutError: If the cross-worker AWX lifecycle lock cannot be acquired.
    """
    with awx_lifecycle_lock(client=ocp_admin_client):
        register_awx_worker(client=ocp_admin_client)
        mark_awx_install_owner(client=ocp_admin_client)
        deploy_awx_via_helm()
        create_awx_instance(ocp_admin_client=ocp_admin_client, namespace=AWX_NAMESPACE)
    wait_for_awx_ready(ocp_admin_client=ocp_admin_client, namespace=AWX_NAMESPACE)
    awx_url = get_awx_route_url(ocp_admin_client=ocp_admin_client, namespace=AWX_NAMESPACE)
    password = get_awx_admin_password(ocp_admin_client=ocp_admin_client, namespace=AWX_NAMESPACE)
    wait_for_awx_api_ready(awx_url=awx_url, username=AWX_ADMIN_USERNAME, password=password)
    try:
        yield awx_url
    finally:
        with awx_lifecycle_lock(client=ocp_admin_client):
            unregister_awx_worker(client=ocp_admin_client)


@pytest.fixture(scope="session")
def awx_api_token(
    ocp_admin_client: "DynamicClient",
    awx_deployment: str,
) -> str:
    """Create an AWX OAuth2 API token.

    Args:
        ocp_admin_client: OpenShift admin client.
        awx_deployment: AWX route URL.

    Returns:
        str: OAuth2 token for AWX API authentication.
    """
    password = get_awx_admin_password(
        ocp_admin_client=ocp_admin_client,
        namespace=AWX_NAMESPACE,
    )
    return create_awx_auth_token(
        awx_url=awx_deployment,
        username=AWX_ADMIN_USERNAME,
        password=password,
    )


@pytest.fixture(scope="session")
def awx_job_templates(
    awx_deployment: str,
    awx_api_token: str,
) -> dict[str, int]:
    """Create AWX project and job templates for pre/post hooks.

    Creates a project from the mtv-aap-test-playbooks git repo,
    waits for SCM sync, then creates two job templates.

    Args:
        awx_deployment: AWX route URL.
        awx_api_token: AWX API token.

    Returns:
        dict[str, int]: Mapping of hook type to template ID:
            ``{"pre_hook": <id>, "post_hook": <id>}``
    """
    project_id = create_awx_project(
        awx_url=awx_deployment,
        token=awx_api_token,
        name=AWX_PROJECT_NAME,
        scm_url=AAP_TEST_PLAYBOOKS_REPO,
    )
    wait_for_awx_project_sync(
        awx_url=awx_deployment,
        token=awx_api_token,
        project_id=project_id,
    )

    inventory_id = create_awx_inventory(
        awx_url=awx_deployment,
        token=awx_api_token,
    )

    pre_hook_id = create_awx_job_template(
        awx_url=awx_deployment,
        token=awx_api_token,
        name=AWX_PREHOOK_TEMPLATE_NAME,
        project_id=project_id,
        playbook=AWX_PREHOOK_PLAYBOOK,
        inventory_id=inventory_id,
    )
    post_hook_id = create_awx_job_template(
        awx_url=awx_deployment,
        token=awx_api_token,
        name=AWX_POSTHOOK_TEMPLATE_NAME,
        project_id=project_id,
        playbook=AWX_POSTHOOK_PLAYBOOK,
        inventory_id=inventory_id,
    )

    return {"pre_hook": pre_hook_id, "post_hook": post_hook_id}


@pytest.fixture(scope="session")
def aap_mtv_settings(
    ocp_admin_client: "DynamicClient",
    fixture_store: dict[str, Any],
    mtv_namespace: str,
    session_uuid: str,
    awx_deployment: str,
    awx_api_token: str,
) -> Generator[None, None, None]:
    """Configure MTV to use AWX for AAP hooks.

    Creates a session-unique AWX token Secret in the MTV namespace and patches
    the ForkliftController with ``aap_token_secret_name``, ``aap_url``, and
    ``aap_insecure_skip_verify``.

    Args:
        ocp_admin_client: OpenShift admin client.
        fixture_store: Fixture store for resource tracking.
        mtv_namespace: MTV operator namespace (e.g., openshift-mtv).
        session_uuid: Session UUID for unique resource naming.
        awx_deployment: AWX route URL.
        awx_api_token: AWX OAuth2 token.

    Yields:
        None
    """
    token_secret_name = f"{session_uuid}-awx-aap"
    create_aap_token_secret(
        ocp_admin_client=ocp_admin_client,
        fixture_store=fixture_store,
        namespace=mtv_namespace,
        token=awx_api_token,
        name=token_secret_name,
    )

    forklift_controller = ForkliftController(
        client=ocp_admin_client,
        name=FORKLIFT_CONTROLLER_NAME,
        namespace=mtv_namespace,
        ensure_exists=True,
    )

    LOGGER.info(
        f"Patching ForkliftController with aap_url='{awx_deployment}', "
        f"aap_token_secret_name='{token_secret_name}', and aap_insecure_skip_verify=true"
    )
    editor = ResourceEditor(
        patches={
            forklift_controller: {
                "spec": {
                    "aap_url": awx_deployment,
                    "aap_token_secret_name": token_secret_name,
                    "aap_insecure_skip_verify": "true",
                }
            }
        }
    )
    editor.update(backup_resources=True)
    try:
        forklift_controller.wait_for_condition(
            status=forklift_controller.Condition.Status.TRUE,
            condition=forklift_controller.Condition.Type.SUCCESSFUL,
            timeout=300,
        )
        yield
    finally:
        editor.restore()


@pytest.fixture(scope="class")
def aap_hook_refs(
    prepared_plan: dict[str, Any],
    fixture_store: dict[str, Any],
    ocp_admin_client: "DynamicClient",
    target_namespace: str,
    awx_job_templates: dict[str, int],
    aap_mtv_settings: None,
) -> None:
    """Create AAP Hook CRs and store references in prepared_plan.

    Creates pre-hook and post-hook Hook CRs with ``spec.aap.jobTemplateId``
    pointing to AWX job templates. Stores ``_pre_hook_name``,
    ``_pre_hook_namespace``, ``_post_hook_name``, ``_post_hook_namespace``
    in the prepared_plan dict for ``test_create_plan`` to use.

    Args:
        prepared_plan: The prepared migration plan dict.
        fixture_store: Fixture store for resource tracking.
        ocp_admin_client: OpenShift admin client.
        target_namespace: Target namespace for Hook CR creation.
        awx_job_templates: AWX job template IDs.
        aap_mtv_settings: Ensures MTV is configured with AAP token.
    """
    for hook_type, template_key in [("pre", "pre_hook"), ("post", "post_hook")]:
        hook_config: dict[str, Any] = {
            "aap_job_template_id": awx_job_templates[template_key],
        }
        hook_name, hook_namespace = create_hook_for_plan(
            hook_config=hook_config,
            hook_type=hook_type,
            fixture_store=fixture_store,
            ocp_admin_client=ocp_admin_client,
            target_namespace=target_namespace,
        )
        prepared_plan[f"_{hook_type}_hook_name"] = hook_name
        prepared_plan[f"_{hook_type}_hook_namespace"] = hook_namespace
