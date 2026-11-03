from __future__ import annotations

import threading
from types import TracebackType
from typing import TYPE_CHECKING, Any

from kubernetes.dynamic.exceptions import ApiException
from ocp_resources.plan import Plan
from ocp_resources.pod import Pod
from simple_logger.logger import get_logger
from urllib3.exceptions import HTTPError

from exceptions.exceptions import MigrationNotFoundError
from utilities.mtv_migration import (
    CONVERSION_POD_LABEL_SELECTOR,
    get_conversion_pods,
    get_migration_for_plan,
)

if TYPE_CHECKING:
    from kubernetes.dynamic import ResourceInstance

# Kubernetes watch streams are re-armed in bounded segments so a lost connection
# cannot leave the observer thread stuck for the whole migration, and so stopping
# the observer ends the stream within one segment instead of idling the test.
_WATCH_SEGMENT_TIMEOUT_SECONDS = 5

# A watch that keeps failing to open or stream gives up after this many
# consecutive failures; the failures stay recorded in the observer's error
# list so the final assertion surfaces them instead of the loop spinning
# silently for the rest of the migration.
_WATCH_MAX_CONSECUTIVE_FAILURES = 5

LOGGER = get_logger(__name__)


class ConvertorPodObserver:
    """Verify virt-v2v convertor pod selector inheritance and placement during a migration.

    Usable as an ``on_status_poll`` callback for ``execute_migration``: on every poll it
    looks up the migration's convertor pods in the Plan target namespace, asserts each
    carries the Plan's convertor node selector, and records pods scheduled on the
    expected node. Convertor pods are short-lived, so the checks must run while the
    migration is in progress, not after it completes.

    A background Kubernetes watch on the migration's convertor pods runs alongside the
    status polls so a pod that is scheduled and removed between two polls is still
    observed: watch events are checked with the same rules and recorded in the same
    observation set used by the final assertion.
    """

    def __init__(
        self,
        plan: Plan,
        expected_node_name: str,
        expected_node_selector: dict[str, str],
    ) -> None:
        """Initialize the observer for one migration.

        Args:
            plan (Plan): Plan whose Migration and convertor pods are observed.
            expected_node_name (str): Worker node the convertor pods must run on.
            expected_node_selector (dict[str, str]): Node selector the convertor pods
                must inherit from the Plan's convertorNodeSelector.
        """
        self._plan = plan
        self._expected_node_name = expected_node_name
        self._expected_node_selector = expected_node_selector
        self._migration_uid: str | None = None
        self._observed_pod_names: set[str] = set()
        self._watch_stop_event = threading.Event()
        self._watch_thread: threading.Thread | None = None
        self._watch_errors: list[str] = []
        self._pod_validation_errors: list[str] = []
        self._watch_consecutive_failures = 0

    def __enter__(self) -> ConvertorPodObserver:
        """Enter the context manager owning the background pod watch.

        Returns:
            ConvertorPodObserver: This observer, so the caller can pass it to
                ``execute_migration`` and assert inside the ``with`` block.
        """
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Stop the background pod watch on context exit, even when the migration failed.

        Args:
            exc_type: Exception type raised in the ``with`` block, if any.
            exc_value: Exception value raised in the ``with`` block, if any.
            traceback: Traceback of the exception raised in the ``with`` block, if any.
        """
        self.stop()

    def stop(self) -> None:
        """Stop the background convertor pod watch; safe to call more than once.

        Sets the stop event and joins the watch thread until it exits, so a stopped
        observer never leaves a watch stream running for the rest of the session.
        """
        self._watch_stop_event.set()
        if self._watch_thread is not None:
            # The stream segment times out within _WATCH_SEGMENT_TIMEOUT_SECONDS and
            # the thread exits as soon as the loop re-checks the stop event.
            self._watch_thread.join(timeout=_WATCH_SEGMENT_TIMEOUT_SECONDS * 2)

    def __call__(self, status: str) -> None:
        """Check convertor pod selector and placement for the current migration state.

        Starts the background pod watch once the Migration UID is known, then checks the
        currently listed convertor pods.

        Args:
            status (str): Current migration status from the status poll.

        Raises:
            AssertionError: If a convertor pod omits the Plan selector or runs on another node.
            ValueError: If a convertor pod snapshot has malformed metadata.
        """
        if status == Plan.Status.FAILED:
            return

        if self._migration_uid is None:
            try:
                migration = get_migration_for_plan(plan=self._plan)
            except MigrationNotFoundError:
                return
            self._migration_uid = migration.instance.metadata.uid
            self._start_watch()

        for pod in get_conversion_pods(
            client=self._plan.client,
            namespace=self._plan.instance.spec.targetNamespace,
            migration_uid=self._migration_uid,
        ):
            self._check_pod(pod=pod)

    def assert_convertor_pod_observed(self) -> None:
        """Require at least one convertor pod to have been observed on the expected node.

        Stops the background watch first so every event received during the migration is
        included in the observation set and reported errors. A transient watch failure
        with pods still observed via the status polls passes; only a missing pod makes
        the assertion fail, and then the watch failures are reported alongside it so a
        broken watch is not mistaken for an absence of pods. Pods observed by the watch
        that failed selector/node-placement validation always fail the assertion, even
        when other pods were observed successfully.

        Raises:
            AssertionError: If no convertor pod was observed running on the expected node
                (watch failures are included in the message when present), or if a pod
                observed by the watch failed selector/node-placement validation.
        """
        self.stop()
        assert not self._pod_validation_errors, (
            f"virt-v2v convertor pods observed by the background watch of Plan "
            f"'{self._plan.name}' failed selector/node-placement validation: "
            f"{self._pod_validation_errors}"
        )
        watch_failures = f" (the background pod watch also failed: {self._watch_errors})" if self._watch_errors else ""
        assert self._observed_pod_names, (
            f"No virt-v2v convertor pod was observed on node '{self._expected_node_name}' during "
            f"migration of Plan '{self._plan.name}'{watch_failures}"
        )

    def _start_watch(self) -> None:
        """Start the background convertor pod watch thread once."""
        if self._watch_thread is not None:
            return
        self._watch_thread = threading.Thread(
            target=self._watch_conversion_pods,
            name=f"convertor-pod-watch-{self._plan.name}",
            daemon=True,
        )
        self._watch_thread.start()

    def _watch_conversion_pods(self) -> None:
        """Watch convertor pod events for the whole migration, re-arming on segment timeout.

        Every event — including pods deleted between status polls — is checked with the
        same rules as the poll path and recorded in the shared observation set. Watch-open
        and stream-iteration failures (API or connection errors) are recorded, logged, and
        the watch is re-established on the next loop segment; after consecutive failures
        the thread gives up and the recorded errors surface in the final assertion. Pods
        that fail validation are recorded separately and always fail the final assertion.

        The DynamicClient discovery call (resources.get) is made inside the loop so its
        failures are recorded like watch failures and the watch re-arms; unexpected
        exception types intentionally terminate the thread (broad catching is forbidden
        in this repo), with the recorded errors surfacing in the final assertion and the
        1s status-poll path remaining the primary observation source.
        """
        while not self._watch_stop_event.is_set():
            try:
                # ocp_resources Pod requires name and namespace to instantiate, and the watch is
                # cluster-scoped by label selector, so use the DynamicClient resources API —
                # the same call ocp_resources Resource.full_api() makes for Pod.
                pod_api = self._plan.client.resources.get(api_version=Pod.api_version, kind=Pod.kind)
                for event in pod_api.watch(
                    namespace=self._plan.instance.spec.targetNamespace,
                    label_selector=f"{CONVERSION_POD_LABEL_SELECTOR},migration={self._migration_uid}",
                    timeout=_WATCH_SEGMENT_TIMEOUT_SECONDS,
                ):
                    if self._watch_stop_event.is_set():
                        return
                    try:
                        self._check_pod(pod=event["object"])
                    except (AssertionError, ValueError) as exp:
                        self._pod_validation_errors.append(str(exp))
            except (ApiException, HTTPError, ConnectionError) as exp:
                self._watch_consecutive_failures += 1
                self._watch_errors.append(f"watch failure: {exp}")
                LOGGER.warning(
                    f"Convertor pod watch for Plan '{self._plan.name}' failed "
                    f"({self._watch_consecutive_failures}/{_WATCH_MAX_CONSECUTIVE_FAILURES}): {exp}"
                )
                if self._watch_consecutive_failures >= _WATCH_MAX_CONSECUTIVE_FAILURES:
                    return
            else:
                # A segment that streamed to its timeout without raising is healthy
                # (empty segments are the common case between pod changes), so any
                # successfully completed segment resets the consecutive-failure count.
                self._watch_consecutive_failures = 0

    def _check_pod(self, pod: ResourceInstance) -> None:
        """Assert one convertor pod snapshot inherits the selector and runs on the expected node.

        Args:
            pod (ResourceInstance): Raw convertor pod snapshot.

        Raises:
            AssertionError: If the pod omits the Plan selector or runs on another node.
            ValueError: If the pod snapshot has a malformed spec, nodeSelector, or nodeName.
        """
        # Raw Kubernetes API payload: the field shapes are not statically known.
        pod_dict: dict[str, Any] = pod.to_dict()
        pod_name = pod_dict.get("metadata", {}).get("name")
        pod_spec = pod_dict.get("spec") or {}
        if not isinstance(pod_spec, dict):
            raise ValueError(f"convertor pod '{pod_name}' has a malformed spec: {pod_spec!r}")

        pod_node_selector = pod_spec.get("nodeSelector") or {}
        if not isinstance(pod_node_selector, dict):
            raise ValueError(f"convertor pod '{pod_name}' has a malformed nodeSelector: {pod_node_selector!r}")
        assert all(pod_node_selector.get(key) == value for key, value in self._expected_node_selector.items()), (
            f"virt-v2v convertor pod '{pod_name}' did not inherit Plan convertor "
            f"node selector {self._expected_node_selector}; pod nodeSelector: {pod_node_selector}"
        )

        node_name = pod_spec.get("nodeName")
        if node_name is not None and (not isinstance(node_name, str) or not node_name):
            raise ValueError(f"convertor pod '{pod_name}' has a malformed nodeName: {node_name!r}")
        if not node_name:
            return

        assert node_name == self._expected_node_name, (
            f"virt-v2v convertor pod '{pod_name}' ran on '{node_name}', expected '{self._expected_node_name}'"
        )
        self._observed_pod_names.add(pod_name)
