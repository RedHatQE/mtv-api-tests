"""
AWX/AAP deployment and API utility functions for MTV hook integration testing.

This module provides functions to deploy AWX on OpenShift via Helm,
interact with the AWX REST API (projects, job templates, tokens),
and verify AAP hook execution after migration.
"""

from __future__ import annotations

import base64
import subprocess
import time
import urllib.parse
from typing import TYPE_CHECKING, Any

import requests
from kubernetes.dynamic.exceptions import NotFoundError
from ocp_resources.pod import Pod
from ocp_resources.route import Route
from ocp_resources.secret import Secret
from simple_logger.logger import get_logger

from utilities.resources import create_and_store_resource

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

LOGGER = get_logger(__name__)

AWX_NAMESPACE: str = "awx"
AWX_INSTANCE_NAME: str = "awx-openshift"
AWX_ADMIN_USERNAME: str = "admin"
AWX_DEFAULT_ORGANIZATION_ID: int = 1  # AWX auto-creates a "Default" organization with ID 1
AAP_TEST_PLAYBOOKS_REPO: str = "https://github.com/gwencasey96/mtv-aap-test-playbooks"
AAP_TEST_PLAYBOOKS_BRANCH: str = "main"
AWX_PROJECT_NAME: str = "mtv-aap-test-playbooks"
AWX_PREHOOK_TEMPLATE_NAME: str = "mtv-pre-hook"
AWX_POSTHOOK_TEMPLATE_NAME: str = "mtv-post-hook"
AWX_PREHOOK_PLAYBOOK: str = "pre_hook_integration_example.yml"
AWX_POSTHOOK_PLAYBOOK: str = "post_hook_integration_example.yml"
AWX_HELM_REPO_NAME: str = "awx-operator-helm"
AWX_HELM_REPO_URL: str = "https://ansible-community.github.io/awx-operator-helm/"
AWX_HELM_RELEASE_NAME: str = "awx-operator"
# CephFS required for AWX — projects PVC needs RWX (multiple pods), postgres needs filesystem mode.
# Ceph RBD (used for VM migration storage_class) does not support RWX for filesystem volumes.
AWX_PROJECTS_STORAGE_CLASS: str = "ocs-storagecluster-cephfs"
AWX_POSTGRES_STORAGE_CLASS: str = "ocs-storagecluster-cephfs"
AAP_TOKEN_SECRET_NAME: str = "awx-aap"  # Arbitrary name; referenced by ForkliftController aap_token_secret_name


def _awx_api_request(
    method: str,
    awx_url: str,
    endpoint: str,
    token: str | None = None,
    auth: tuple[str, str] | None = None,
    json_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Make an authenticated request to the AWX REST API.

    Args:
        method: HTTP method (GET, POST, etc.).
        awx_url: AWX base URL (https://...).
        endpoint: API endpoint path (e.g., "/api/v2/tokens/").
        token: OAuth2 bearer token for authentication.
        auth: Basic auth tuple (username, password) as alternative to token.
        json_data: JSON body for POST/PUT requests.

    Returns:
        dict[str, Any]: Parsed JSON response.

    Raises:
        requests.HTTPError: If the API returns a non-2xx status.
    """
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"{awx_url}{endpoint}"
    response = requests.request(
        method=method,
        url=url,
        headers=headers,
        auth=auth,
        json=json_data,
        verify=False,
        timeout=30,
    )
    if not response.ok:
        body = response.text[:500] if response.text else "(empty)"
        LOGGER.error(f"AWX API {method} {url} failed ({response.status_code}): {body}")
    response.raise_for_status()
    if response.content:
        return response.json()
    return {}


def _run_shell(command: str, timeout: int = 600) -> str:
    """Run a shell command and return stdout.

    Args:
        command: Shell command string.
        timeout: Timeout in seconds.

    Returns:
        str: Command stdout.

    Raises:
        subprocess.CalledProcessError: If command exits with non-zero status.
        subprocess.TimeoutExpired: If command exceeds timeout.
    """
    LOGGER.info(f"Running: {command}")
    result = subprocess.run(
        command,
        shell=True,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.stdout.strip()


def is_awx_installed() -> bool:
    """Check if AWX Operator is already installed via Helm.

    Returns:
        bool: True if the AWX Helm release exists in the awx namespace.

    Raises:
        subprocess.CalledProcessError: If Helm command fails.
    """
    output = _run_shell(f"helm list -n {AWX_NAMESPACE} -q")
    return AWX_HELM_RELEASE_NAME in output


def deploy_awx_via_helm() -> None:
    """Deploy AWX Operator via Helm chart if not already installed.

    Skips installation if the Helm release already exists.

    Raises:
        subprocess.CalledProcessError: If Helm installation fails.
    """
    if is_awx_installed():
        LOGGER.info("AWX Operator already installed via Helm, skipping deployment")
        return

    LOGGER.info("Adding AWX Operator Helm repository")
    _run_shell(f"helm repo add {AWX_HELM_REPO_NAME} {AWX_HELM_REPO_URL} --force-update")
    _run_shell("helm repo update")

    LOGGER.info(f"Installing AWX Operator into namespace '{AWX_NAMESPACE}'")
    _run_shell(
        f"helm install {AWX_HELM_RELEASE_NAME} {AWX_HELM_REPO_NAME}/awx-operator "
        f"--namespace {AWX_NAMESPACE} --create-namespace --wait --timeout 5m"
    )


def create_awx_instance(
    ocp_admin_client: "DynamicClient",
    namespace: str,
) -> None:
    """Create an AWX custom resource instance if not already present.

    Uses the dynamic client directly because AWX is a third-party CRD
    (awx.ansible.com/v1beta1) installed at runtime by the Helm operator —
    the wrapper's CRD discovery requires the CRD to be pre-registered,
    and the kind 'AWX' doesn't match Python class naming conventions.
    Cleanup is handled by namespace deletion in teardown_awx().

    Args:
        ocp_admin_client: OpenShift admin client.
        namespace: Namespace where AWX operator is installed.
    """
    try:
        awx_api = ocp_admin_client.resources.get(api_version="awx.ansible.com/v1beta1", kind="AWX")
        awx_api.get(name=AWX_INSTANCE_NAME, namespace=namespace)
        LOGGER.info(f"AWX instance '{AWX_INSTANCE_NAME}' already exists, skipping creation")
        return
    except NotFoundError:
        pass

    awx_body: dict[str, Any] = {
        "apiVersion": "awx.ansible.com/v1beta1",
        "kind": "AWX",
        "metadata": {
            "name": AWX_INSTANCE_NAME,
            "namespace": namespace,
        },
        "spec": {
            "ingress_type": "Route",
            "postgres_storage_class": AWX_POSTGRES_STORAGE_CLASS,
            "postgres_storage_requirement": "4Gi",
            "projects_persistence": True,
            "projects_storage_class": AWX_PROJECTS_STORAGE_CLASS,
            "projects_storage_size": "4Gi",
        },
    }
    LOGGER.info(f"Creating AWX instance '{AWX_INSTANCE_NAME}' in namespace '{namespace}'")
    ocp_admin_client.resources.get(api_version="awx.ansible.com/v1beta1", kind="AWX").create(body=awx_body)


def wait_for_awx_ready(
    ocp_admin_client: "DynamicClient",
    namespace: str,
    timeout: int = 900,
) -> None:
    """Wait for all AWX pods to be in Running state.

    Args:
        ocp_admin_client: OpenShift admin client.
        namespace: AWX namespace.
        timeout: Maximum wait time in seconds.

    Raises:
        TimeoutError: If AWX pods are not ready within timeout.
    """
    LOGGER.info(f"Waiting for AWX pods to be ready in namespace '{namespace}' (timeout={timeout}s)")
    expected_prefixes = (f"{AWX_INSTANCE_NAME}-postgres", f"{AWX_INSTANCE_NAME}-task", f"{AWX_INSTANCE_NAME}-web")
    deadline = time.time() + timeout

    while time.time() < deadline:
        pods = list(Pod.get(client=ocp_admin_client, namespace=namespace))
        all_components_ready = all(
            any(pod.name.startswith(prefix) and pod.instance.status.phase == "Running" for pod in pods)
            for prefix in expected_prefixes
        )
        if all_components_ready:
            LOGGER.info("All AWX components (postgres, task, web) are Running")
            return
        time.sleep(15)

    raise TimeoutError(f"AWX pods not ready within {timeout}s in namespace '{namespace}'")


def get_awx_admin_password(
    ocp_admin_client: "DynamicClient",
    namespace: str,
) -> str:
    """Retrieve the AWX admin password from the auto-generated Secret.

    Args:
        ocp_admin_client: OpenShift admin client.
        namespace: AWX namespace.

    Returns:
        str: The admin password.

    Raises:
        ValueError: If the secret does not exist or has no password field.
    """
    secret_name = f"{AWX_INSTANCE_NAME}-admin-password"
    secret = Secret(
        client=ocp_admin_client,
        name=secret_name,
        namespace=namespace,
        ensure_exists=True,
    )
    password_b64 = secret.instance.data.get("password")
    if not password_b64:
        raise ValueError(f"Secret '{secret_name}' in namespace '{namespace}' has no 'password' field")
    return base64.b64decode(password_b64).decode("utf-8")


def get_awx_route_url(
    ocp_admin_client: "DynamicClient",
    namespace: str,
) -> str:
    """Get the AWX web UI route URL.

    Args:
        ocp_admin_client: OpenShift admin client.
        namespace: AWX namespace.

    Returns:
        str: The AWX route URL (https://...).

    Raises:
        ValueError: If no AWX route exists.
    """
    routes = list(Route.get(client=ocp_admin_client, namespace=namespace))
    for route in routes:
        if AWX_INSTANCE_NAME in route.name:
            host = route.instance.spec.host
            LOGGER.info(f"AWX route URL: https://{host}")
            return f"https://{host}"

    raise ValueError(f"No AWX route found in namespace '{namespace}'")


def wait_for_awx_api_ready(
    awx_url: str,
    username: str,
    password: str,
    timeout: int = 600,
) -> None:
    """Wait for AWX API to be fully operational.

    AWX pods may be Running before the API can handle write operations.
    The ping endpoint responds early but database migrations may still
    be running. This function polls the organizations endpoint with
    authentication to verify the API is fully ready.

    Args:
        awx_url: AWX base URL.
        username: AWX admin username.
        password: AWX admin password.
        timeout: Maximum wait time in seconds.

    Raises:
        TimeoutError: If AWX API is not ready within timeout.
    """
    LOGGER.info(f"Waiting for AWX API to be fully ready at {awx_url} (timeout={timeout}s)")
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            response = requests.get(
                f"{awx_url}/api/v2/organizations/",
                auth=(username, password),
                verify=False,
                timeout=10,
            )
            if response.ok and response.json().get("count", 0) > 0:
                LOGGER.info("AWX API is fully ready (organizations endpoint responding)")
                return
        except (requests.ConnectionError, requests.Timeout, ValueError):
            pass
        time.sleep(15)

    raise TimeoutError(f"AWX API not fully ready within {timeout}s at {awx_url}")


def _find_awx_resource_by_name(
    awx_url: str,
    token: str,
    endpoint: str,
    name: str,
) -> dict[str, Any] | None:
    """Find an AWX resource by name.

    Args:
        awx_url: AWX base URL.
        token: AWX API auth token.
        endpoint: API endpoint (e.g., "/api/v2/projects/").
        name: Resource name to search for.

    Returns:
        dict[str, Any] | None: The resource dict if found, None otherwise.
    """
    encoded_name = urllib.parse.quote(name, safe="")
    response = _awx_api_request(
        method="GET",
        awx_url=awx_url,
        endpoint=f"{endpoint}?name={encoded_name}",
        token=token,
    )
    results = response.get("results", [])
    if results:
        return results[0]
    return None


def create_awx_auth_token(
    awx_url: str,
    username: str,
    password: str,
) -> str:
    """Create an AWX OAuth2 personal access token.

    Args:
        awx_url: AWX base URL.
        username: AWX admin username.
        password: AWX admin password.

    Returns:
        str: The OAuth2 token string.

    Raises:
        requests.HTTPError: If token creation fails.
    """
    LOGGER.info("Creating AWX OAuth2 token")
    response = _awx_api_request(
        method="POST",
        awx_url=awx_url,
        endpoint="/api/v2/tokens/",
        auth=(username, password),
        json_data={"description": "mtv-api-tests", "scope": "write"},
    )
    token = response["token"]
    LOGGER.info("AWX OAuth2 token created successfully")
    return token


def create_awx_project(
    awx_url: str,
    token: str,
    name: str,
    scm_url: str,
    retries: int = 20,
    retry_interval: int = 15,
) -> int:
    """Create an AWX project from a git SCM URL, or return existing project ID.

    Retries on 500 errors because AWX may still be initializing after deployment.

    Args:
        awx_url: AWX base URL.
        token: AWX API auth token.
        name: Project name.
        scm_url: Git repository URL for playbooks.
        retries: Number of retry attempts on 500 errors.
        retry_interval: Seconds between retries.

    Returns:
        int: The project ID (created or existing).

    Raises:
        requests.HTTPError: If project creation fails after all retries.
    """
    existing = _find_awx_resource_by_name(awx_url=awx_url, token=token, endpoint="/api/v2/projects/", name=name)
    if existing:
        LOGGER.info(f"AWX project '{name}' already exists with ID {existing['id']}")
        return existing["id"]

    LOGGER.info(f"Creating AWX project '{name}' from '{scm_url}'")
    for attempt in range(1, retries + 1):
        try:
            response = _awx_api_request(
                method="POST",
                awx_url=awx_url,
                endpoint="/api/v2/projects/",
                token=token,
                json_data={
                    "name": name,
                    "scm_type": "git",
                    "scm_url": scm_url,
                    "scm_branch": AAP_TEST_PLAYBOOKS_BRANCH,
                    "organization": AWX_DEFAULT_ORGANIZATION_ID,
                },
            )
            project_id: int = response["id"]
            LOGGER.info(f"AWX project created with ID {project_id}")
            return project_id
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 500 and attempt < retries:
                LOGGER.warning(f"AWX project creation returned 500, retrying ({attempt}/{retries})...")
                time.sleep(retry_interval)
                existing = _find_awx_resource_by_name(
                    awx_url=awx_url, token=token, endpoint="/api/v2/projects/", name=name
                )
                if existing:
                    LOGGER.info(f"AWX project '{name}' found after retry with ID {existing['id']}")
                    return existing["id"]
            elif e.response is not None and e.response.status_code == 400:
                existing = _find_awx_resource_by_name(
                    awx_url=awx_url, token=token, endpoint="/api/v2/projects/", name=name
                )
                if existing:
                    LOGGER.info(f"AWX project '{name}' already exists with ID {existing['id']}")
                    return existing["id"]
                raise
            else:
                raise

    raise requests.HTTPError(
        f"AWX project '{name}' creation failed after {retries} retries (url={awx_url}, scm_url={scm_url})"
    )


def wait_for_awx_project_sync(
    awx_url: str,
    token: str,
    project_id: int,
    timeout: int = 300,
) -> None:
    """Wait for an AWX project to finish its initial SCM sync.

    Args:
        awx_url: AWX base URL.
        token: AWX API auth token.
        project_id: AWX project ID.
        timeout: Timeout in seconds.

    Raises:
        TimeoutError: If project sync does not complete within timeout.
        ValueError: If project sync fails.
    """
    LOGGER.info(f"Waiting for AWX project {project_id} SCM sync (timeout={timeout}s)")
    deadline = time.time() + timeout

    while time.time() < deadline:
        response = _awx_api_request(
            method="GET",
            awx_url=awx_url,
            endpoint=f"/api/v2/projects/{project_id}/",
            token=token,
        )
        status = response.get("status", "")
        if status == "successful":
            LOGGER.info(f"AWX project {project_id} sync completed successfully")
            return
        if status == "failed":
            raise ValueError(f"AWX project {project_id} sync failed: {response.get('summary_fields', {})}")
        time.sleep(10)

    raise TimeoutError(f"AWX project {project_id} sync did not complete within {timeout}s")


def create_awx_inventory(
    awx_url: str,
    token: str,
    name: str = "Default",
) -> int:
    """Create an AWX inventory, or return existing inventory ID.

    AWX job templates require an inventory even if the playbook only
    runs on localhost.

    Args:
        awx_url: AWX base URL.
        token: AWX API auth token.
        name: Inventory name.

    Returns:
        int: The inventory ID (created or existing).

    Raises:
        requests.HTTPError: If inventory creation fails.
    """
    existing = _find_awx_resource_by_name(awx_url=awx_url, token=token, endpoint="/api/v2/inventories/", name=name)
    if existing:
        LOGGER.info(f"AWX inventory '{name}' already exists with ID {existing['id']}")
        return existing["id"]

    LOGGER.info(f"Creating AWX inventory '{name}'")
    response = _awx_api_request(
        method="POST",
        awx_url=awx_url,
        endpoint="/api/v2/inventories/",
        token=token,
        json_data={
            "name": name,
            "organization": AWX_DEFAULT_ORGANIZATION_ID,
        },
    )
    inventory_id: int = response["id"]
    LOGGER.info(f"AWX inventory '{name}' created with ID {inventory_id}")
    return inventory_id


def create_awx_job_template(
    awx_url: str,
    token: str,
    name: str,
    project_id: int,
    playbook: str,
    inventory_id: int,
) -> int:
    """Create an AWX job template for a playbook, or return existing template ID.

    Args:
        awx_url: AWX base URL.
        token: AWX API auth token.
        name: Job template name.
        project_id: Project ID containing the playbook.
        playbook: Playbook filename within the project.
        inventory_id: AWX inventory ID (required by AWX API).

    Returns:
        int: The job template ID (created or existing).

    Raises:
        requests.HTTPError: If job template creation fails.
    """
    existing = _find_awx_resource_by_name(awx_url=awx_url, token=token, endpoint="/api/v2/job_templates/", name=name)
    if existing:
        LOGGER.info(f"AWX job template '{name}' already exists with ID {existing['id']}")
        return existing["id"]

    LOGGER.info(f"Creating AWX job template '{name}' for playbook '{playbook}'")
    response = _awx_api_request(
        method="POST",
        awx_url=awx_url,
        endpoint="/api/v2/job_templates/",
        token=token,
        json_data={
            "name": name,
            "project": project_id,
            "playbook": playbook,
            "inventory": inventory_id,
            "organization": AWX_DEFAULT_ORGANIZATION_ID,
            "ask_variables_on_launch": True,
        },
    )
    template_id: int = response["id"]
    LOGGER.info(f"AWX job template '{name}' created with ID {template_id}")
    return template_id


def create_aap_token_secret(
    ocp_admin_client: "DynamicClient",
    fixture_store: dict[str, Any],
    namespace: str,
    token: str,
    name: str = AAP_TOKEN_SECRET_NAME,
) -> Secret:
    """Create a Kubernetes Secret with the AWX OAuth2 token for MTV.

    The ForkliftController uses this secret to authenticate with AWX
    when triggering AAP hooks during migration.

    Args:
        ocp_admin_client: OpenShift admin client.
        fixture_store: Fixture store for resource tracking.
        namespace: MTV operator namespace (e.g., openshift-mtv).
        token: AWX OAuth2 token string.
        name: Secret name. Defaults to AAP_TOKEN_SECRET_NAME.

    Returns:
        Secret: The created Kubernetes Secret.
    """
    LOGGER.info(f"Creating AAP token secret '{name}' in namespace '{namespace}'")
    return create_and_store_resource(
        client=ocp_admin_client,
        fixture_store=fixture_store,
        resource=Secret,
        name=name,
        namespace=namespace,
        string_data={"token": token},
    )


def teardown_awx() -> None:
    """Remove AWX deployment via Helm and delete namespace.

    Attempts both Helm uninstall and namespace deletion. Logs
    failures but does not raise — teardown errors should not
    mark passing tests as failures.
    """
    LOGGER.info("Tearing down AWX deployment")
    try:
        _run_shell(f"helm uninstall {AWX_HELM_RELEASE_NAME} --namespace {AWX_NAMESPACE}")
    except subprocess.CalledProcessError as e:
        LOGGER.warning(f"Helm uninstall failed (stderr: {e.stderr}): {e}")
    try:
        _run_shell(f"oc delete namespace {AWX_NAMESPACE} --wait=false")
    except subprocess.CalledProcessError as e:
        LOGGER.warning(f"Failed to delete namespace '{AWX_NAMESPACE}' (stderr: {e.stderr}): {e}")
