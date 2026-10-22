from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING

from ocp_resources.node import Node
from ocp_resources.plan import Plan
from ocp_resources.pod import Pod
from ocp_resources.resource import ResourceEditor
from ocp_resources.utils.constants import TIMEOUT_10MINUTES, TIMEOUT_2MINUTES

from exceptions.exceptions import MigrationNotFoundError
from utilities.mtv_migration import get_migration_for_plan
from utilities.worker_node_selection import (
    get_node_labels,
    get_ready_schedulable_worker_nodes,
    select_node_by_available_memory,
)

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient, ResourceInstance


_IMPORTER_POD_LABEL_SELECTOR = "app=containerized-data-importer,cdi.kubevirt.io=importer"
_CONVERSION_POD_LABEL_SELECTOR = "forklift.app=virt-v2v"


def _get_pod_snapshots(plan: Plan, namespace: str, label_selector: str) -> list[ResourceInstance]:
    """Return individual Pod snapshots from a raw Pod query.

    Args:
        plan (Plan): Plan whose client is used to query Pods.
        namespace (str): Namespace containing the Pods.
        label_selector (str): Kubernetes label selector for the Pods.

    Returns:
        list[ResourceInstance]: Individual Pod snapshots from one API response.
    """
    return list(
        Pod.get(
            client=plan.client,
            namespace=namespace,
            label_selector=label_selector,
            raw=True,
        )
    )


@dataclass
class ConvertorNodeSelectorTarget:
    """Plan selector, eligible workers, and the worker enabled during recovery."""

    selector: dict[str, str]
    candidate_worker_nodes: list[Node]
    worker_node: Node | None = None
    label_editor: ResourceEditor | None = None


class ConvertorPodPlacementObserver:
    """Capture and validate warm migration importer and conversion pod placement."""

    def __init__(
        self,
        plan: Plan,
        target: ConvertorNodeSelectorTarget,
        on_status_poll: Callable[[str], None] | None = None,
    ) -> None:
        """Initialize a status-poll callback for one warm migration.

        Args:
            plan (Plan): Warm Plan whose Migration and pods are observed.
            target (ConvertorNodeSelectorTarget): Selector and worker required for pod placement.
            on_status_poll (Callable[[str], None] | None): Existing callback to invoke on every status poll.
        """
        self._plan = plan
        self._target = target
        self._on_status_poll = on_status_poll
        self._migration_uid: str | None = None
        self._importer_pod_names: set[str] = set()
        self._conversion_pod_names: set[str] = set()
        self._initial_importer_names: set[str] = set()
        self._migration_observation_started_at: float | None = None
        self._first_importer_seen_at: float | None = None
        self._worker_labeled_at: float | None = None

    def __call__(self, status: str) -> None:
        """Validate pod selectors and record pods scheduled during a status poll.

        Args:
            status (str): Current migration status.

        Raises:
            AssertionError: If importers do not appear or recover in time, or a pod omits the selector or runs on
                another worker.
        """
        if self._on_status_poll is not None:
            self._on_status_poll(status)

        if status == Plan.Status.FAILED:
            return

        if self._migration_uid is None:
            try:
                migration = get_migration_for_plan(plan=self._plan)
            except MigrationNotFoundError:
                return
            self._migration_uid = migration.instance.metadata.uid

        if self._migration_observation_started_at is None:
            self._migration_observation_started_at = monotonic()

        namespace = self._plan.instance.spec.targetNamespace
        importer_pods = _get_pod_snapshots(
            plan=self._plan,
            namespace=namespace,
            label_selector=f"{_IMPORTER_POD_LABEL_SELECTOR},migration={self._migration_uid}",
        )
        conversion_pods = _get_pod_snapshots(
            plan=self._plan,
            namespace=namespace,
            label_selector=f"{_CONVERSION_POD_LABEL_SELECTOR},migration={self._migration_uid}",
        )
        self._check_importer_readiness_deadline(importer_pods=importer_pods, status=status)
        if self._target.worker_node is None:
            self._recover_when_importer_unschedulable(importer_pods=importer_pods)
        self._record_placements(
            pods=importer_pods,
            observed_pod_names=self._importer_pod_names,
            pod_type="CDI importer",
        )
        self._record_placements(
            pods=conversion_pods,
            observed_pod_names=self._conversion_pod_names,
            pod_type="virt-v2v conversion",
        )
        self._check_recovery_scheduling_deadline()

    def assert_placements_observed(self) -> None:
        """Require unschedulable importer recovery and both pod types' placement to be observed.

        Raises:
            AssertionError: If no importer became unschedulable or either pod type was not observed scheduling.
        """
        assert self._initial_importer_names, "No CDI importer pod was observed Pending and Unschedulable"
        assert self._target.worker_node is not None, "No worker was labeled to recover CDI importer scheduling"
        assert self._initial_importer_names <= self._importer_pod_names, (
            "Initially unschedulable CDI importer pod(s) were not observed scheduled after recovery. "
            f"Missing: {sorted(self._initial_importer_names - self._importer_pod_names)}"
        )
        assert self._importer_pod_names, "No CDI importer pod placement was observed during warm migration"
        assert self._conversion_pod_names, "No virt-v2v conversion pod placement was observed during warm migration"

    def _recover_when_importer_unschedulable(self, importer_pods: list[ResourceInstance]) -> None:
        """Label a compatible worker only after observing importer pods unschedulable.

        Args:
            importer_pods (list[ResourceInstance]): Current importer pod snapshots.

        Raises:
            AssertionError: If no importer becomes unschedulable in time or one schedules before label recovery.
        """
        for pod in importer_pods:
            self._assert_selector(pod=pod, pod_type="CDI importer")
            node_name = pod.get("spec", {}).get("nodeName")
            assert not node_name, (
                f"CDI importer pod '{pod.metadata.name}' scheduled on '{node_name}' before the matching "
                "worker label was applied"
            )

        if not importer_pods:
            return

        unschedulable_pods = [pod for pod in importer_pods if _is_unschedulable(pod=pod)]
        if unschedulable_pods:
            self._initial_importer_names.update(pod.metadata.name for pod in unschedulable_pods)
            self._label_compatible_worker(importer_pods=importer_pods)
            return

        assert self._first_importer_seen_at is not None
        if monotonic() - self._first_importer_seen_at >= TIMEOUT_2MINUTES:
            raise AssertionError(
                f"CDI importer pods appeared but none reported PodScheduled=Unschedulable within "
                f"{TIMEOUT_2MINUTES}s; observed: {_get_importer_pod_states(pods=importer_pods)}"
            )

    def _label_compatible_worker(self, importer_pods: list[ResourceInstance]) -> None:
        """Apply the Plan selector to a worker compatible with CDI's other selectors.

        Args:
            importer_pods (list[ResourceInstance]): Pending importer pod snapshots.
        """
        worker_node = _select_compatible_worker_node(
            client=self._plan.client,
            candidate_worker_nodes=self._target.candidate_worker_nodes,
            importer_pods=importer_pods,
            selector=self._target.selector,
        )
        self._target.worker_node = worker_node
        self._target.label_editor = ResourceEditor(
            patches={worker_node: {"metadata": {"labels": self._target.selector}}}
        )
        self._target.label_editor.update(backup_resources=True)
        self._worker_labeled_at = monotonic()

    def _check_importer_readiness_deadline(self, importer_pods: list[ResourceInstance], status: str) -> None:
        """Fail if no importer appears within the migration readiness window.

        Args:
            importer_pods (list[ResourceInstance]): Current importer pod snapshots.
            status (str): Current migration status.

        Raises:
            AssertionError: If no importer pod is observed before the readiness deadline.
        """
        if self._migration_observation_started_at is None or self._first_importer_seen_at is not None:
            return

        if importer_pods:
            self._first_importer_seen_at = monotonic()
            return

        if monotonic() - self._migration_observation_started_at >= TIMEOUT_10MINUTES:
            raise AssertionError(
                f"No CDI importer pod was observed within {TIMEOUT_10MINUTES}s while Migration "
                f"'{self._migration_uid}' had status '{status}'"
            )

    def _check_recovery_scheduling_deadline(self) -> None:
        """Fail if initially unschedulable importer pods do not schedule after labeling a worker.

        Raises:
            AssertionError: If any initially unschedulable importer remains unassigned past the recovery deadline.
        """
        if (
            self._worker_labeled_at is None
            or self._initial_importer_names <= self._importer_pod_names
            or monotonic() - self._worker_labeled_at < TIMEOUT_2MINUTES
        ):
            return

        worker_name = self._target.worker_node.name if self._target.worker_node else "<unknown>"
        missing_importers = sorted(self._initial_importer_names - self._importer_pod_names)
        raise AssertionError(
            f"Initially unschedulable CDI importer pod(s) {missing_importers} did not schedule within "
            f"{TIMEOUT_2MINUTES}s after applying ConvertorNodeSelector {self._target.selector} to worker "
            f"'{worker_name}'"
        )

    def _record_placements(
        self,
        pods: list[ResourceInstance],
        observed_pod_names: set[str],
        pod_type: str,
    ) -> None:
        """Check selector inheritance and record pods running on the selected worker.

        Args:
            pods (list[ResourceInstance]): Pod snapshots to inspect.
            observed_pod_names (set[str]): Names of pods already validated; new names are added in place.
            pod_type (str): Human-readable pod type for assertion messages.

        Raises:
            AssertionError: If a pod omits the Plan selector or runs on a different worker.
        """
        for pod in pods:
            pod_name = pod.metadata.name
            self._assert_selector(pod=pod, pod_type=pod_type)
            pod_spec = pod.get("spec", {})
            node_name = pod_spec.get("nodeName")
            if not node_name or pod_name in observed_pod_names:
                continue

            assert self._target.worker_node is not None, (
                f"{pod_type.capitalize()} pod '{pod_name}' was scheduled before any worker matched "
                f"Plan selector {self._target.selector}"
            )
            assert node_name == self._target.worker_node.name, (
                f"{pod_type.capitalize()} pod '{pod_name}' ran on '{node_name}', "
                f"expected '{self._target.worker_node.name}'"
            )
            observed_pod_names.add(pod_name)

    def _assert_selector(self, pod: ResourceInstance, pod_type: str) -> None:
        """Verify that a pod snapshot includes the Plan selector.

        Args:
            pod (ResourceInstance): Pod snapshot to inspect.
            pod_type (str): Human-readable pod type for assertion messages.

        Raises:
            AssertionError: If the pod does not inherit the Plan selector.
        """
        pod_selector = pod.get("spec", {}).get("nodeSelector") or {}
        assert all(pod_selector.get(key) == value for key, value in self._target.selector.items()), (
            f"{pod_type.capitalize()} pod '{pod.metadata.name}' did not inherit Plan selector {self._target.selector}"
        )


def _select_compatible_worker_node(
    client: DynamicClient,
    candidate_worker_nodes: list[Node],
    importer_pods: list[ResourceInstance],
    selector: dict[str, str],
) -> Node:
    """Select a Ready worker that matches CDI selectors besides the Plan's unique label.

    Args:
        client (DynamicClient): OpenShift client used to select by available memory.
        candidate_worker_nodes (list[Node]): Ready, schedulable workers gathered before migration.
        importer_pods (list[ResourceInstance]): Pending pods whose node selectors must be honored.
        selector (dict[str, str]): Plan ConvertorNodeSelector to exclude from existing-label matching.

    Returns:
        Node: A compatible worker to label for scheduling recovery.

    Raises:
        AssertionError: If no worker satisfies CDI's other node selector entries.
    """
    required_selectors = [
        {key: value for key, value in (pod.get("spec", {}).get("nodeSelector") or {}).items() if key not in selector}
        for pod in importer_pods
    ]
    candidate_worker_names = {node.name for node in candidate_worker_nodes}
    current_worker_nodes = get_ready_schedulable_worker_nodes(ocp_client=client)
    compatible_workers = [
        node
        for node in current_worker_nodes
        if node.name in candidate_worker_names
        if all(
            all(get_node_labels(node=node).get(key) == value for key, value in required_selector.items())
            for required_selector in required_selectors
        )
    ]
    assert compatible_workers, (
        "No Ready, schedulable worker satisfies CDI importer selectors other than ConvertorNodeSelector. "
        f"Required selectors: {required_selectors}; "
        f"candidate workers: {[node.name for node in current_worker_nodes]}"
    )
    selected_node_name = select_node_by_available_memory(
        ocp_admin_client=client,
        worker_nodes=[node.name for node in compatible_workers],
    )
    return next(node for node in compatible_workers if node.name == selected_node_name)


def _is_unschedulable(pod: ResourceInstance) -> bool:
    """Check whether a pod is Pending with a scheduler Unschedulable condition.

    Args:
        pod (ResourceInstance): Pod snapshot to inspect.

    Returns:
        bool: Whether the scheduler has reported this pending pod as unschedulable.
    """
    status = pod.get("status", {})
    return status.get("phase") == Pod.Status.PENDING and any(
        condition.get("type") == "PodScheduled"
        and condition.get("status") == "False"
        and condition.get("reason") == "Unschedulable"
        for condition in status.get("conditions") or []
    )


def _get_importer_pod_states(pods: list[ResourceInstance]) -> list[dict[str, str | None]]:
    """Return phase, node assignment, and scheduling reason for importer diagnostics.

    Args:
        pods (list[ResourceInstance]): Importer snapshots to summarize.

    Returns:
        list[dict[str, str | None]]: Product-visible scheduling fields for each importer.
    """
    return [
        {
            "name": pod.metadata.name,
            "phase": pod.get("status", {}).get("phase"),
            "nodeName": pod.get("spec", {}).get("nodeName"),
            "reason": next(
                (
                    condition.get("reason")
                    for condition in pod.get("status", {}).get("conditions") or []
                    if condition.get("type") == "PodScheduled"
                ),
                None,
            ),
        }
        for pod in pods
    ]
