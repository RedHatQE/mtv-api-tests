"""qcow2 overlay lifecycle verification for virt-v2v-in-place conversions (MTV-5644)."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import TYPE_CHECKING, NotRequired, Protocol, TypedDict, runtime_checkable

from kubernetes.dynamic.exceptions import ApiException
from ocp_resources.plan import Plan
from ocp_resources.pod import Pod
from simple_logger.logger import get_logger
from timeout_sampler import TimeoutExpiredError, TimeoutSampler
from urllib3.exceptions import MaxRetryError, ProtocolError, ReadTimeoutError

from utilities.copyoffload_migration import get_migration_uid
from utilities.mtv_migration import get_migration_for_plan

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

LOGGER = get_logger(__name__)

_CONVERSION_POD_LABEL_SELECTOR = "forklift.app=virt-v2v"
_VIRT_V2V_CONTAINER_NAME = "virt-v2v"
_VM_ID_LABEL = "vmID"

_OVERLAY_CREATED_RE = re.compile(r"Created overlay (\S+\.qcow2)(?=\s|$)")
_OVERLAY_COMMITTED_RE = re.compile(r"Committed and removed overlay (\S+\.qcow2)(?=\s|$)")
_OVERLAY_DISCARDED_RE = re.compile(r"Discarded overlay (\S+\.qcow2)")
_INPLACE_COMMAND_RE = re.compile(r"Building command: virt-v2v-in-place")
_POST_SUCCESS_LOG_CAPTURE_TIMEOUT_SECONDS = 60  # Allow Kubernetes to report container termination after Plan success.
_LOG_CAPTURE_POLL_INTERVAL_SECONDS = 1
_TRANSIENT_API_STATUSES = {
    HTTPStatus.REQUEST_TIMEOUT,
    HTTPStatus.TOO_MANY_REQUESTS,
    HTTPStatus.INTERNAL_SERVER_ERROR,
    HTTPStatus.BAD_GATEWAY,
    HTTPStatus.SERVICE_UNAVAILABLE,
    HTTPStatus.GATEWAY_TIMEOUT,
}


class _ContainerState(TypedDict, total=False):
    """Container state fields used to determine log availability and completion."""

    running: dict[str, object]
    terminated: dict[str, object]


class _ContainerStatus(TypedDict):
    """Container identity and optional state from the Kubernetes pod status."""

    name: str
    state: NotRequired[_ContainerState | None]


@runtime_checkable
class _FieldPairs(Protocol):
    """Iterable key-value pairs exposed by a Kubernetes dynamic resource field."""

    def __iter__(self) -> Iterator[tuple[str, object]]: ...


def _as_field_mapping(value: object, field_name: str, pod_name: str) -> dict[str, object]:
    """Validate and convert a pod status field to a standard mapping.

    Kubernetes dynamic ResourceField values iterate over key-value pairs but dynamically provide
    their ``get`` method, which cannot be reliably checked by a runtime Protocol.

    Args:
        value (object): External Kubernetes status field.
        field_name (str): Name used in the validation error.
        pod_name (str): Pod name used in the validation error.

    Returns:
        dict[str, object]: Validated status-field mapping.

    Raises:
        ValueError: If the external field is not a mapping or a dynamic resource field.
    """
    if isinstance(value, dict):
        mapping = value
    elif isinstance(value, _FieldPairs):
        try:
            mapping = dict(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"Pod '{pod_name}': {field_name} is not a mapping") from error
    else:
        raise ValueError(f"Pod '{pod_name}': {field_name} is not a mapping")
    if not all(isinstance(key, str) for key in mapping):
        raise ValueError(f"Pod '{pod_name}': {field_name} is not a valid mapping")
    return mapping


@dataclass
class OverlayPodLogs:
    """Latest conversion log snapshot for one VM, with completion evidence."""

    vm_id: str
    pod_name: str
    content: str = ""
    complete: bool = False


@dataclass
class OverlayLogCapture:
    """Capture migration-scoped conversion logs while preserving another polling callback."""

    ocp_admin_client: DynamicClient
    plan: Plan
    target_namespace: str
    on_status_poll: Callable[[str], None] | None = None
    migration_uid: str | None = field(default=None, init=False)
    expected_vm_ids: set[str] = field(default_factory=set, init=False)
    pods: dict[str, OverlayPodLogs] = field(default_factory=dict, init=False)

    def capture(self, status: str) -> None:
        """Capture conversion logs on each migration poll, including the success poll.

        Args:
            status (str): Current migration status passed to the existing callback.

        Raises:
            ApiException: If overlay capture encounters a non-transient API error.
            ValueError: If migration metadata or a conversion pod VM ID is missing.
        """
        if self.on_status_poll is not None:
            self.on_status_poll(status)
        if not status or status == Plan.Status.FAILED:
            return
        try:
            self.capture_conversion_logs()
        except ApiException as error:
            if error.status not in _TRANSIENT_API_STATUSES:
                raise
            LOGGER.warning(f"Transient API error while capturing overlay logs: {error}")
        except (ConnectionError, MaxRetryError, ProtocolError, ReadTimeoutError, TimeoutError) as error:
            LOGGER.warning(f"Transient connection error while capturing overlay logs: {error}")

    def capture_conversion_logs(self) -> None:
        """Capture terminated virt-v2v container logs for the active migration.

        Raises:
            ValueError: If migration metadata or a conversion pod VM ID is missing.
        """
        if self.migration_uid is None:
            migration_uid = get_migration_for_plan(plan=self.plan).instance.metadata.uid
            if not isinstance(migration_uid, str) or not migration_uid:
                raise ValueError(f"Migration for Plan '{self.plan.name}' has no valid UID")
            expected_vm_ids = self._get_expected_vm_ids()
            self.migration_uid = migration_uid
            self.expected_vm_ids = expected_vm_ids
        for instance in Pod.get(
            client=self.ocp_admin_client,
            namespace=self.target_namespace,
            label_selector=f"{_CONVERSION_POD_LABEL_SELECTOR},migration={self.migration_uid}",
            raw=True,
        ):
            pod_uid = instance.metadata.uid
            if not isinstance(pod_uid, str) or not pod_uid:
                raise ValueError(f"Conversion pod '{instance.metadata.name}' has no valid UID")
            pod_labels = instance.metadata.labels or {}
            vm_id = pod_labels.get(_VM_ID_LABEL)
            if not isinstance(vm_id, str) or not vm_id:
                raise ValueError(f"Conversion pod '{instance.metadata.name}' has no valid '{_VM_ID_LABEL}' label")
            if vm_id not in self.expected_vm_ids:
                raise ValueError(f"Conversion pod '{instance.metadata.name}' references unexpected VM '{vm_id}'")
            pod_status = instance.get("status") or {}
            self._capture_pod(
                pod_uid=pod_uid,
                vm_id=vm_id,
                pod_name=instance.metadata.name,
                containers=pod_status.get("containerStatuses") or [],
            )

    def _get_expected_vm_ids(self) -> set[str]:
        """Return the VM IDs configured in the migration Plan.

        Returns:
            set[str]: Configured VM IDs.

        Raises:
            ValueError: If the Plan has no configured VMs or a VM has no ID.
        """
        vms = self.plan.instance.spec.vms
        if not vms:
            raise ValueError(f"Plan '{self.plan.name}' has no configured VMs")
        vm_ids: set[str] = set()
        for vm in vms:
            try:
                vm_id = vm["id"]
            except KeyError as error:
                raise ValueError(f"Plan '{self.plan.name}' has a VM without a valid ID") from error
            if not isinstance(vm_id, str) or not vm_id:
                raise ValueError(f"Plan '{self.plan.name}' has a VM without a valid ID")
            if vm_id in vm_ids:
                raise ValueError(f"Plan '{self.plan.name}' has duplicate VM ID '{vm_id}'")
            vm_ids.add(vm_id)
        return vm_ids

    def wait_for_complete_logs(self) -> None:
        """Wait for terminated conversion containers and capture their complete logs.

        Raises:
            ValueError: If complete conversion logs cannot be captured after migration success.
            ApiException: If conversion pod retrieval or log capture fails.
        """
        try:
            for complete in TimeoutSampler(
                wait_timeout=_POST_SUCCESS_LOG_CAPTURE_TIMEOUT_SECONDS,
                sleep=_LOG_CAPTURE_POLL_INTERVAL_SECONDS,
                func=self._capture_complete_logs,
                exceptions_dict={},
            ):
                if complete:
                    return
        except TimeoutExpiredError as error:
            if error.last_exp is not None:
                raise error.last_exp from error
            incomplete_pods = [snapshot.pod_name for snapshot in self.pods.values() if not snapshot.complete]
            missing_vm_ids = self.expected_vm_ids - self._captured_vm_ids()
            raise ValueError(
                "Complete virt-v2v logs were not captured after migration success: "
                f"missing VM IDs={sorted(missing_vm_ids)}; incomplete pods={sorted(incomplete_pods)}"
            ) from error

    def _capture_complete_logs(self) -> bool:
        """Capture current conversion logs and report whether every Plan VM has complete evidence.

        Returns:
            bool: True when every configured Plan VM ID has a captured pod and every capture is complete.
        """
        self.capture_conversion_logs()
        return self._captured_vm_ids() == self.expected_vm_ids and all(
            snapshot.complete for snapshot in self.pods.values()
        )

    def _captured_vm_ids(self) -> set[str]:
        """Return VM IDs represented by captured conversion pod attempts.

        Returns:
            set[str]: Captured VM IDs.
        """
        return {snapshot.vm_id for snapshot in self.pods.values()}

    def _capture_pod(
        self,
        pod_uid: str,
        vm_id: str,
        pod_name: str,
        containers: list[_ContainerStatus],
    ) -> None:
        """Refresh a pod snapshot until its conversion container has terminated.

        Args:
            pod_uid (str): UID from the conversion pod metadata.
            vm_id (str): VM ID from the conversion pod label.
            pod_name (str): Name of the conversion pod.
            containers (list[_ContainerStatus]): Container identities and states from the pod status.

        Raises:
            ApiException: If log retrieval fails with an API status other than 404.
            ValueError: If the terminated container state is malformed or has a nonzero exit code.
        """
        snapshot = self.pods.setdefault(pod_uid, OverlayPodLogs(vm_id=vm_id, pod_name=pod_name))
        if snapshot.complete:
            return
        for container in containers:
            if container["name"] == _VIRT_V2V_CONTAINER_NAME:
                state = container.get("state")
                if state is None:
                    return
                state_mapping = _as_field_mapping(state, "container state", pod_name)
                terminated = state_mapping.get("terminated")
                if terminated is None:
                    return
                terminated_mapping = _as_field_mapping(terminated, "terminated container state", pod_name)
                exit_code = terminated_mapping.get("exitCode")
                if type(exit_code) is not int:
                    raise ValueError(f"Pod '{pod_name}': terminated container state has no integer exit code")
                if exit_code != 0:
                    raise ValueError(f"Pod '{pod_name}': virt-v2v container exited with code {exit_code}")
                pod = Pod(client=self.ocp_admin_client, namespace=self.target_namespace, name=pod_name)
                try:
                    snapshot.content = pod.log(container=_VIRT_V2V_CONTAINER_NAME)
                except ApiException as error:
                    if error.status != HTTPStatus.NOT_FOUND:
                        raise
                    # Retain incomplete evidence if cleanup races the log request.
                    return
                snapshot.complete = True
                return


def _verify_inplace_mode(pod_name: str, log_content: str) -> None:
    """Verify the virt-v2v pod used in-place conversion mode.

    Args:
        pod_name (str): Pod name for error messages.
        log_content (str): virt-v2v container log text.

    Raises:
        AssertionError: If virt-v2v-in-place command is not found in logs.
    """
    assert _INPLACE_COMMAND_RE.search(log_content), (
        f"Pod '{pod_name}': 'Building command: virt-v2v-in-place' not found in logs — "
        "migration did not use in-place mode"
    )
    LOGGER.info(f"Pod '{pod_name}': confirmed virt-v2v-in-place mode")


def _extract_overlay_names(
    pod_name: str,
    log_content: str,
    pattern: re.Pattern[str],
    description: str,
) -> set[str]:
    """Extract overlay filenames matching a log pattern.

    Args:
        pod_name (str): Pod name for error messages.
        log_content (str): virt-v2v container log text.
        pattern (re.Pattern[str]): Compiled regex with one capture group for the filename.
        description (str): Human-readable description of the pattern for error messages.

    Returns:
        set[str]: Overlay filenames found in logs.

    Raises:
        AssertionError: If no matches are found.
    """
    filenames = {match.group(1) for match in pattern.finditer(log_content)}
    assert filenames, f"Pod '{pod_name}': no '{description}' messages found in virt-v2v logs"
    LOGGER.info(f"Pod '{pod_name}': found {len(filenames)} '{description}' message(s): {sorted(filenames)}")
    return filenames


def _verify_no_discarded_overlays(pod_name: str, log_content: str) -> None:
    """Verify no overlay files were discarded (discard indicates failure path).

    Args:
        pod_name (str): Pod name for error messages.
        log_content (str): virt-v2v container log text.

    Raises:
        AssertionError: If any 'Discarded overlay' messages are found.
    """
    discarded = {match.group(1) for match in _OVERLAY_DISCARDED_RE.finditer(log_content)}
    assert not discarded, (
        f"Pod '{pod_name}': found 'Discarded overlay' messages for {sorted(discarded)} — "
        "overlays should be committed on success, not discarded"
    )


def _verify_overlay_sets_match(
    pod_name: str,
    created: set[str],
    committed: set[str],
) -> None:
    """Verify every created overlay was committed.

    Args:
        pod_name (str): Pod name for error messages.
        created (set[str]): Overlay filenames from 'Created overlay' messages.
        committed (set[str]): Overlay filenames from 'Committed and removed overlay' messages.

    Raises:
        AssertionError: If the created and committed sets do not match.
    """
    assert created == committed, (
        f"Pod '{pod_name}': overlay mismatch — "
        f"created={sorted(created)}, committed={sorted(committed)}; "
        f"uncommitted={sorted(created - committed)}, unexpected={sorted(committed - created)}"
    )
    LOGGER.info(f"Pod '{pod_name}': all {len(created)} overlay(s) created and committed successfully")


def verify_overlay_lifecycle(capture: OverlayLogCapture) -> None:
    """Verify complete cached conversion logs after a successful migration.

    Args:
        capture (OverlayLogCapture): Logs collected by the migration polling callback.

    Raises:
        ValueError: If migration identity differs or complete conversion logs are missing.
        AssertionError: If overlay lifecycle messages are missing or unexpected.
    """
    migration_uid = get_migration_uid(plan=capture.plan)
    if capture.migration_uid != migration_uid:
        raise ValueError(f"Captured migration '{capture.migration_uid}' does not match completed '{migration_uid}'")
    if capture._captured_vm_ids() != capture.expected_vm_ids:
        raise ValueError(
            f"Captured VM IDs {sorted(capture._captured_vm_ids())} do not match expected {sorted(capture.expected_vm_ids)}"
        )
    if not capture.pods:
        raise ValueError(f"No virt-v2v logs captured for migration '{migration_uid}'")
    for snapshot in capture.pods.values():
        if not snapshot.complete:
            raise ValueError(f"Pod '{snapshot.pod_name}': complete virt-v2v logs were not captured after termination")
        pod_name = snapshot.pod_name
        log_content = snapshot.content
        _verify_inplace_mode(pod_name=pod_name, log_content=log_content)
        created_overlays = _extract_overlay_names(
            pod_name=pod_name,
            log_content=log_content,
            pattern=_OVERLAY_CREATED_RE,
            description="Created overlay",
        )
        committed_overlays = _extract_overlay_names(
            pod_name=pod_name,
            log_content=log_content,
            pattern=_OVERLAY_COMMITTED_RE,
            description="Committed and removed overlay",
        )
        _verify_no_discarded_overlays(pod_name=pod_name, log_content=log_content)
        _verify_overlay_sets_match(pod_name=pod_name, created=created_overlays, committed=committed_overlays)
    LOGGER.info(f"Overlay lifecycle verified for {len(capture.pods)} conversion pod(s)")
