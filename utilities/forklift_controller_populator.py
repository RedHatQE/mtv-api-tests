"""ForkliftController populator in-flight limit helpers for copy-offload tests."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from ocp_resources.deployment import Deployment
from ocp_resources.forklift_controller import ForkliftController
from ocp_resources.resource import ResourceEditor
from simple_logger.logger import get_logger
from timeout_sampler import TimeoutExpiredError, TimeoutSampler

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

LOGGER = get_logger(__name__)

POPULATOR_CONTROLLER_DEPLOYMENT = "forklift-volume-populator-controller"
MAX_POPULATOR_INFLIGHT_ENV = "MAX_POPULATOR_INFLIGHT"


def ensure_secure_shared_lock_dir(lock_dir: Path) -> None:
    """Validate permissions on a cross-worker shared lock directory.

    Args:
        lock_dir (Path): Directory used for pytest-xdist file locks.

    Raises:
        PermissionError: If the directory is a symlink or owned by another user.
    """
    lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    if lock_dir.is_symlink():
        raise PermissionError(
            f"Security error: shared directory {lock_dir} is a symlink. This may indicate a hijack attempt."
        )

    current_uid = os.getuid()
    dir_stat = lock_dir.lstat()
    if dir_stat.st_uid != current_uid:
        raise PermissionError(
            f"Security error: shared directory {lock_dir} is owned by uid {dir_stat.st_uid}, "
            f"expected current user uid {current_uid}. This may indicate a hijack attempt."
        )
    os.chmod(lock_dir, 0o700)


def get_forkliftcontroller_populator_inflight_lock_path() -> Path:
    """Return the cross-worker lock path for ForkliftController populator limit changes.

    Returns:
        Path: File lock path under a secured shared temp directory.
    """
    lock_dir = Path(tempfile.gettempdir()) / "pytest-shared-forklift"
    ensure_secure_shared_lock_dir(lock_dir=lock_dir)
    return lock_dir / "populator-inflight.lock"


def get_populator_inflight_from_deployment(deployment: Deployment) -> str | None:
    """Read MAX_POPULATOR_INFLIGHT from the populator controller deployment.

    Args:
        deployment (Deployment): Populator controller deployment resource.

    Returns:
        str | None: Configured in-flight limit, or None if the env var is absent.
    """
    containers = deployment.instance.spec.template.spec.containers
    for container in containers:
        for env_var in container.env or []:
            if env_var.name == MAX_POPULATOR_INFLIGHT_ENV:
                return env_var.value
    return None


def wait_for_populator_inflight_deployment(
    ocp_admin_client: DynamicClient,
    mtv_namespace: str,
    expected_limit: int,
) -> None:
    """Wait until the populator controller deployment applies the in-flight limit.

    ForkliftController spec changes propagate to the populator-controller Deployment
    asynchronously. Migration must not start until MAX_POPULATOR_INFLIGHT is active.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        mtv_namespace (str): Namespace where the populator controller runs.
        expected_limit (int): Expected MAX_POPULATOR_INFLIGHT value.

    Raises:
        TimeoutError: If the deployment does not reach the expected limit in time.
    """
    expected_value = str(expected_limit)

    def _deployment_ready_with_limit() -> bool:
        current_deployment = Deployment(
            client=ocp_admin_client,
            name=POPULATOR_CONTROLLER_DEPLOYMENT,
            namespace=mtv_namespace,
            ensure_exists=True,
        )
        deployment_status = current_deployment.instance.status
        spec_replicas = current_deployment.instance.spec.replicas or 1
        available_replicas = 0
        if deployment_status:
            available_replicas = deployment_status.availableReplicas or 0
        if available_replicas < spec_replicas:
            return False
        return get_populator_inflight_from_deployment(deployment=current_deployment) == expected_value

    try:
        for _ in TimeoutSampler(
            wait_timeout=300,
            sleep=2,
            func=_deployment_ready_with_limit,
        ):
            if _:
                LOGGER.info(
                    f"Populator controller deployment has {MAX_POPULATOR_INFLIGHT_ENV}={expected_value} "
                    "and is fully available"
                )
                return
    except TimeoutExpiredError as err:
        final_deployment = Deployment(
            client=ocp_admin_client,
            name=POPULATOR_CONTROLLER_DEPLOYMENT,
            namespace=mtv_namespace,
            ensure_exists=True,
        )
        current_limit = get_populator_inflight_from_deployment(deployment=final_deployment)
        raise TimeoutError(
            f"Timed out waiting for {POPULATOR_CONTROLLER_DEPLOYMENT} to apply "
            f"{MAX_POPULATOR_INFLIGHT_ENV}={expected_value} (current={current_limit!r})"
        ) from err


@contextmanager
def populator_inflight_limit(
    forklift_controller: ForkliftController,
    ocp_admin_client: DynamicClient,
    mtv_namespace: str,
    test_limit: int,
    original_deployment_limit: int,
) -> Generator[None, None, None]:
    """Temporarily patch ForkliftController populator in-flight limit and restore on exit.

    Uses ResourceEditor as a context manager so the CR rolls back automatically.
    Waits for the populator deployment to apply each limit change.

    Args:
        forklift_controller (ForkliftController): ForkliftController resource to patch.
        ocp_admin_client (DynamicClient): OpenShift admin client.
        mtv_namespace (str): Namespace where the populator controller runs.
        test_limit (int): Limit to apply for the test (e.g. POPULATOR_INFLIGHT_LIMIT).
        original_deployment_limit (int): MAX_POPULATOR_INFLIGHT value before the test.
    """
    current_cr_limit = getattr(forklift_controller.instance.spec, "controller_max_populator_inflight", None)

    if current_cr_limit == test_limit:
        LOGGER.info(f"ForkliftController controller_max_populator_inflight already {test_limit}")
        wait_for_populator_inflight_deployment(
            ocp_admin_client=ocp_admin_client,
            mtv_namespace=mtv_namespace,
            expected_limit=test_limit,
        )
        yield
        return

    LOGGER.info(
        f"Setting ForkliftController controller_max_populator_inflight from {current_cr_limit!r} to {test_limit}"
    )
    with ResourceEditor(patches={forklift_controller: {"spec": {"controller_max_populator_inflight": test_limit}}}):
        forklift_controller.wait_for_condition(
            status=forklift_controller.Condition.Status.TRUE,
            condition=forklift_controller.Condition.Type.SUCCESSFUL,
            timeout=300,
        )
        wait_for_populator_inflight_deployment(
            ocp_admin_client=ocp_admin_client,
            mtv_namespace=mtv_namespace,
            expected_limit=test_limit,
        )
        yield

    wait_for_populator_inflight_deployment(
        ocp_admin_client=ocp_admin_client,
        mtv_namespace=mtv_namespace,
        expected_limit=original_deployment_limit,
    )
