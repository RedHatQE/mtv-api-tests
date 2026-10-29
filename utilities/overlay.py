"""qcow2 overlay lifecycle verification for virt-v2v-in-place conversions (MTV-5644)."""

from __future__ import annotations

import re
import time
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
from utilities.resume_conversion import CONVERSION_POD_LABEL_SELECTOR

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

_VIRT_V2V_CONTAINER_NAME = "virt-v2v"
_VM_ID_LABEL = "vmID"

_OVERLAY_CREATED_RE = re.compile(r"Created overlay (\S+\.qcow2)(?=\s|$)")
_OVERLAY_COMMITTED_RE = re.compile(r"Committed and removed overlay (\S+\.qcow2)(?=\s|$)")
_OVERLAY_DISCARDED_RE = re.compile(r"Discarded overlay (\S+\.qcow2)")
_INPLACE_COMMAND_RE = re.compile(r"Building command: virt-v2v-in-place")
_POST_SUCCESS_LOG_CAPTURE_TIMEOUT_SECONDS = 60  # Allow Kubernetes to report container termination after Plan success.
_LOG_CAPTURE_POLL_INTERVAL_SECONDS = 1
_LOG_REQUEST_TIMEOUT_SECONDS = 30  # Bound each pod-log request so a stalled read cannot exceed the capture deadline.
_TRANSIENT_API_STATUSES = {
    HTTPStatus.REQUEST_TIMEOUT,
    HTTPStatus.TOO_MANY_REQUESTS,
    HTTPStatus.INTERNAL_SERVER_ERROR,
    HTTPStatus.BAD_GATEWAY,
    HTTPStatus.SERVICE_UNAVAILABLE,
    HTTPStatus.GATEWAY_TIMEOUT,
}
_TRANSIENT_CONNECTION_EXCEPTIONS = (ConnectionError, MaxRetryError, ProtocolError, ReadTimeoutError, TimeoutError)

LOGGER = get_logger(__name__)


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

    def __iter__(self) -> Iterator[tuple[str, object]]:
        """Iterate over the field's key-value pairs.

        Returns:
            Iterator[tuple[str, object]]: Iterator over the string-keyed value pairs
                exposed by the Kubernetes dynamic resource field.
        """
        ...


def _as_field_mapping(status_field: object, field_name: str, pod_name: str) -> dict[str, object]:
    """Validate and convert a pod status field to a standard mapping.

    Kubernetes dynamic ResourceField values iterate over key-value pairs but dynamically provide
    their ``get`` method, which cannot be reliably checked by a runtime Protocol.

    Args:
        status_field (object): External Kubernetes status field.
        field_name (str): Name used in the validation error.
        pod_name (str): Pod name used in the validation error.

    Returns:
        dict[str, object]: Validated status-field mapping.

    Raises:
        ValueError: If the external field is not a mapping or a dynamic resource field.
    """
    if isinstance(status_field, dict):
        mapping = status_field
    elif isinstance(status_field, _FieldPairs):
        try:
            mapping = dict(status_field)
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
    last_capture_error: str | None = field(default=None, init=False)

    def capture(self, status: str) -> None:
        """Capture conversion logs on each migration poll, including the success poll.

        Args:
            status (str): Current migration status passed to the existing callback.

        Returns:
            None: Captured evidence is stored on the instance for later verification.

        Raises:
            ApiException: If overlay capture encounters a non-transient API error.
            ValueError: If migration metadata or a conversion pod VM ID is missing.
        """
        if self.on_status_poll is not None:
            self.on_status_poll(status)
        if not status or status == Plan.Status.FAILED:
            return
        self._capture_conversion_logs_tolerating_transient_errors()

    def _capture_conversion_logs_tolerating_transient_errors(self) -> None:
        """Capture conversion logs, recording transient failures instead of aborting the caller.

        Transient failures are recorded for `wait_for_complete_logs` to report when complete
        evidence stays missing; callers retry by polling `_capture_conversion_logs` again.
        A successful capture clears the recorded failure.

        Returns:
            None

        Raises:
            ApiException: If overlay capture encounters a non-transient API error.
            ValueError: If migration metadata or a conversion pod VM ID is missing.
        """
        try:
            self._capture_conversion_logs()
        except ApiException as error:
            if error.status not in _TRANSIENT_API_STATUSES:
                raise
            self._record_capture_error(error_message=f"Transient API error while capturing overlay logs: {error}")
        except _TRANSIENT_CONNECTION_EXCEPTIONS as error:
            self._record_capture_error(
                error_message=f"Transient connection error while capturing overlay logs: {error}"
            )
        else:
            self.last_capture_error = None

    def _record_capture_error(self, error_message: str) -> None:
        """Record a transient capture failure to report when complete evidence is missing.

        Args:
            error_message (str): Description of the transient failure.

        Returns:
            None
        """
        LOGGER.warning(f"{error_message}")
        self.last_capture_error = error_message

    def _capture_conversion_logs(self) -> None:
        """Capture terminated virt-v2v container logs for the active migration.

        Returns:
            None: Captured logs are stored on the instance, not returned.

        Raises:
            ValueError: If migration metadata is missing, or a conversion pod has malformed
                labels or a missing/invalid VM ID.
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
            label_selector=f"{CONVERSION_POD_LABEL_SELECTOR},migration={self.migration_uid}",
            raw=True,
        ):
            pod_uid = instance.metadata.uid
            if not isinstance(pod_uid, str) or not pod_uid:
                raise ValueError(f"Conversion pod '{instance.metadata.name}' has no valid UID")
            # Dynamic-client pods expose labels as ResourceField objects (dict-like, but not
            # dict subclasses), so convert via to_dict() before the mapping-shape check.
            pod_labels = instance.metadata.labels
            to_dict = getattr(pod_labels, "to_dict", None)
            if callable(to_dict):
                pod_labels = to_dict()
            if not isinstance(pod_labels, dict):
                raise ValueError(f"Conversion pod '{instance.metadata.name}' has malformed labels: {pod_labels!r}")
            vm_id = pod_labels.get(_VM_ID_LABEL)
            if not isinstance(vm_id, str) or not vm_id:
                raise ValueError(f"Conversion pod '{instance.metadata.name}' has no valid '{_VM_ID_LABEL}' label")
            if vm_id not in self.expected_vm_ids:
                raise ValueError(f"Conversion pod '{instance.metadata.name}' references unexpected VM '{vm_id}'")
            pod_status = instance.get("status") or {}
            self._capture_pod(
                pod=Pod(client=self.ocp_admin_client, namespace=self.target_namespace, name=instance.metadata.name),
                pod_uid=pod_uid,
                vm_id=vm_id,
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

        Transient API or connection errors during capture are retried within the capture
        deadline and reported in the timeout error when complete evidence stays missing. Each
        pod-log request is bounded by a request timeout, and a capture completing after the
        deadline is rejected, so a stalled request cannot extend the wait or pass late.

        Raises:
            ValueError: If complete conversion logs are not captured within the post-success
                deadline, whether missing or arriving late.
            ApiException: If conversion pod retrieval or log capture fails.
        """
        start = time.monotonic()
        deadline = start + _POST_SUCCESS_LOG_CAPTURE_TIMEOUT_SECONDS
        try:
            for complete in TimeoutSampler(
                wait_timeout=_POST_SUCCESS_LOG_CAPTURE_TIMEOUT_SECONDS,
                sleep=_LOG_CAPTURE_POLL_INTERVAL_SECONDS,
                func=self._capture_complete_logs,
                exceptions_dict={},
            ):
                if complete:
                    if time.monotonic() <= deadline:
                        return
                    raise ValueError(
                        "Complete virt-v2v logs were captured only after the "
                        f"{_POST_SUCCESS_LOG_CAPTURE_TIMEOUT_SECONDS}-second post-success capture deadline "
                        f"(elapsed {time.monotonic() - start:.1f}s); late evidence is rejected"
                    )
        except TimeoutExpiredError as error:
            if error.last_exp is not None:
                raise error.last_exp from error
            incomplete_pods = [snapshot.pod_name for snapshot in self.pods.values() if not snapshot.complete]
            missing_vm_ids = self.expected_vm_ids - self._captured_vm_ids()
            failure_reason = (
                "Complete virt-v2v logs were not captured after migration success: "
                f"missing VM IDs={sorted(missing_vm_ids)}; incomplete pods={sorted(incomplete_pods)}"
            )
            if self.last_capture_error is not None:
                failure_reason += f"; last capture error: {self.last_capture_error}"
            raise ValueError(failure_reason) from error

    def _capture_complete_logs(self) -> bool:
        """Capture current conversion logs and report whether every Plan VM has complete evidence.

        Transient capture errors are tolerated so the caller can retry within the post-success
        capture window; non-transient errors propagate immediately.

        Returns:
            bool: True when migration identity is initialized and every configured Plan VM ID has
                a captured pod with every capture complete.

        Raises:
            ApiException: If overlay capture encounters a non-transient API error.
            ValueError: If migration metadata, a conversion pod VM ID, or a terminated container
                state is invalid, or the virt-v2v container exited with a nonzero code.
        """
        self._capture_conversion_logs_tolerating_transient_errors()
        if self.migration_uid is None or not self.expected_vm_ids:
            # Before migration identity initialization, empty captured and expected VM ID sets
            # compare equal and `all()` over no pods is vacuously true.
            return False
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
        pod: Pod,
        pod_uid: str,
        vm_id: str,
        containers: list[_ContainerStatus],
    ) -> None:
        """Refresh a pod snapshot until its conversion container has terminated.

        Args:
            pod (Pod): Conversion pod resource used for log retrieval.
            pod_uid (str): UID from the conversion pod metadata.
            vm_id (str): VM ID from the conversion pod label.
            containers (list[_ContainerStatus]): Container identities and states from the pod status.

        Raises:
            ApiException: If log retrieval fails with an API status other than 404.
            ValueError: If the terminated container state is malformed or has a nonzero exit code.
        """
        snapshot = self.pods.setdefault(pod_uid, OverlayPodLogs(vm_id=vm_id, pod_name=pod.name))
        if snapshot.complete:
            return
        for container in containers:
            if container["name"] == _VIRT_V2V_CONTAINER_NAME:
                state = container.get("state")
                if state is None:
                    return
                state_mapping = _as_field_mapping(state, "container state", pod.name)
                terminated = state_mapping.get("terminated")
                if terminated is None:
                    return
                terminated_mapping = _as_field_mapping(terminated, "terminated container state", pod.name)
                exit_code = terminated_mapping.get("exitCode")
                if type(exit_code) is not int:
                    raise ValueError(f"Pod '{pod.name}': terminated container state has no integer exit code")
                if exit_code != 0:
                    raise ValueError(f"Pod '{pod.name}': virt-v2v container exited with code {exit_code}")
                try:
                    snapshot.content = pod.log(
                        container=_VIRT_V2V_CONTAINER_NAME, _request_timeout=_LOG_REQUEST_TIMEOUT_SECONDS
                    )
                except ApiException as error:
                    if error.status != HTTPStatus.NOT_FOUND:
                        raise
                    # Retain incomplete evidence if cleanup races the log request.
                    return
                snapshot.complete = True
                return


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


def verify_overlay_lifecycle(capture: OverlayLogCapture) -> None:
    """Verify complete cached conversion logs after a successful migration.

    Args:
        capture (OverlayLogCapture): Logs collected by the migration polling callback.

    Returns:
        None

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
        assert _INPLACE_COMMAND_RE.search(log_content), (
            f"Pod '{pod_name}': 'Building command: virt-v2v-in-place' not found in logs — "
            "migration did not use in-place mode"
        )
        LOGGER.info(f"Pod '{pod_name}': confirmed virt-v2v-in-place mode")
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
        discarded_overlays = {match.group(1) for match in _OVERLAY_DISCARDED_RE.finditer(log_content)}
        assert not discarded_overlays, (
            f"Pod '{pod_name}': found 'Discarded overlay' messages for {sorted(discarded_overlays)} — "
            "overlays should be committed on success, not discarded"
        )
        assert created_overlays == committed_overlays, (
            f"Pod '{pod_name}': overlay mismatch — "
            f"created={sorted(created_overlays)}, committed={sorted(committed_overlays)}; "
            f"uncommitted={sorted(created_overlays - committed_overlays)}, "
            f"unexpected={sorted(committed_overlays - created_overlays)}"
        )
        LOGGER.info(f"Pod '{pod_name}': all {len(created_overlays)} overlay(s) created and committed successfully")
    LOGGER.info(f"Overlay lifecycle verified for {len(capture.pods)} conversion pod(s)")
