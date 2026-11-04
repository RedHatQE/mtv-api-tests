"""Bounded, never-raising cluster state snapshots for failure and session-start evidence.

Every probe reads the cluster through ``ocp_resources`` wrappers on the run's ``DynamicClient``:
collection never shells out to the ``oc`` CLI and never builds a kubeconfig or writes a token to
disk. Keep it that way — a new probe belongs to a wrapper class, not to a subprocess.
"""

from __future__ import annotations

import json
import os
import re
import socket
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any, Literal

from kubernetes.dynamic.exceptions import ApiException, DynamicApiError
from urllib3.exceptions import MaxRetryError, ProtocolError
from ocp_resources.cluster_operator import ClusterOperator
from ocp_resources.cluster_service_version import ClusterServiceVersion
from ocp_resources.cluster_version import ClusterVersion
from ocp_resources.datavolume import DataVolume
from ocp_resources.event import Event
from ocp_resources.exceptions import ClientWithBasicAuthError
from ocp_resources.image_content_source_policy import ImageContentSourcePolicy
from ocp_resources.image_digest_mirror_set import ImageDigestMirrorSet
from ocp_resources.machine_config_pool import MachineConfigPool
from ocp_resources.node import Node
from ocp_resources.persistent_volume import PersistentVolume
from ocp_resources.persistent_volume_claim import PersistentVolumeClaim
from ocp_resources.pod import Pod
from ocp_resources.resource import NamespacedResource, Resource
from ocp_resources.storage_class import StorageClass
from ocp_resources.user import User
from simple_logger.logger import get_logger

from exceptions.exceptions import ClusterStateProbeTimeout
from utilities.data_collector import data_collector_path as shared_data_collector_path
from utilities.data_collector import redact_secret, utc_timestamp
from utilities.utils import get_cluster_client, resolve_cluster_credential

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

LOGGER = get_logger(name=__name__)

# A snapshot must never delay the failure it documents: the storage scope alone can run dozens of
# probes, so a hanging API server would otherwise cost minutes before the error shows.
_SNAPSHOT_DEADLINE_SECONDS = 120
# One probe's recorded output is capped so a busy cluster cannot inflate the artifact (and the
# memory used to build it) exactly when the disk is already the failing resource.
_MAX_PROBE_OUTPUT_BYTES = 512 * 1024
_TRUNCATION_MARKER = "[truncated: {omitted} bytes omitted]"
_MCO_NAMESPACE = "openshift-machine-config-operator"
_STORAGE_NAMESPACE = "openshift-storage"
_MTV_NAMESPACE = "openshift-mtv"
_CNV_NAMESPACE = "openshift-cnv"
_MACHINE_CONFIG_OPERATOR = "machine-config"
_CLUSTER_VERSION_NAME = "version"
# There is no wrapper class for the cluster-wide image config in this wrapper release, so it is
# read through the client's generic kind API instead of a subprocess.
_CLUSTER_IMAGE_API_VERSION = "config.openshift.io/v1"
_CLUSTER_IMAGE_KIND = "Image"
_CLUSTER_IMAGE_NAME = "cluster"
# ``~`` is the API's self-subject name for the authenticated user, the documented ``oc whoami``.
_CURRENT_USER_NAME = "~"
# Bounded because the probe asks for every container of every replica: the stderr of a busy
# multi-replica provisioner is otherwise large enough to fill the artifact disk.
_CSI_LOG_TAIL_LINES = 300
_MCO_LOG_TAIL_LINES = 500
# A snapshot of one pod's current and previous log covers an operator restart; daemonsets that are
# not part of the MCO or the ceph-csi path would only add noise.
_MCO_POD_PREFIXES = ("machine-config-operator-", "machine-config-daemon-", "machine-config-controller-")
_CSI_POD_PREFIXES = ("csi-rbdplugin-provisioner-", "csi-rbdplugin-")
# Non-Bound PVC bodies carry their own event history and can be tens of KiB each on a full disk;
# the listing above already names every PVC with its phase.
_MAX_PENDING_PVC_DUMPS = 20
# Events of a stalled migration can be hours old, so the lookback covers the whole wait budget.
_EVENT_LOOKBACK_SECONDS = 24 * 60 * 60
_GIT_PACKED_REFS_NAME = "packed-refs"
_HTTP_BAD_REQUEST = 400
# The wrapper exposes no per-request timeout, so a stalled API or OAuth endpoint would block past the
# snapshot deadline and delay the error being reported. The socket default bounds every request made
# inside the context; it is restored on exit.
_REQUEST_SOCKET_TIMEOUT_SECONDS = 30


@contextmanager
def _bounded_requests() -> Iterator[None]:
    """Bound every network request made inside the context with a socket timeout.

    Yields:
        None: The context only applies and restores the socket timeout.
    """
    previous_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(_REQUEST_SOCKET_TIMEOUT_SECONDS)
    try:
        yield
    finally:
        socket.setdefaulttimeout(previous_timeout)


_NODE_ROLE_LABEL_PREFIX = "node-role.kubernetes.io/"
# Only used to reach a kind's API handle through its wrapper class; nothing is read for it.
_KIND_HANDLE_NAME = "cluster-state-probe"
_PROBE_LABEL_PREFIX = "### "
_SECTION_SEPARATOR = "\n\n"
_SNAPSHOT_DIRECTORY = "cluster-state"
_SESSION_START_DIRECTORY = "session-start"
_SESSION_START_CLUSTER_NAME = "cluster.txt"
_SESSION_START_RUN_IDENTITY_NAME = "run-identity.txt"
_GIT_HEAD_FALLBACK = "unknown"
_GIT_REF_PREFIX = "ref: "
_REASON_SLUG_MAX_LENGTH = 60
_REASON_SLUG_SEPARATOR = "-"
_UNSPECIFIED_REASON = "unspecified"
_TC_ARGUMENT_PREFIX = "--tc="
_TC_ARGUMENT_FLAG = "--tc"
_TC_KEY_SEPARATOR = ":"
_REDACTED_VALUE = "***"
_OUTPUT_ENCODING = "utf-8"
# Credential-ish ``--tc`` keys whose value must never reach an artifact.
_CREDENTIAL_KEY_PATTERN = re.compile(r"password|token|secret|api_key|apikey|credential", re.IGNORECASE)
_URL_USERINFO_PATTERN = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)[^/@\s]+@")
# The wrapper raises NotImplementedError when a kind is absent from the cluster's API discovery
# (IDMS on a cluster that only ships ICSP, for example): an empty probe, not a crash. A connection
# that cannot be established at all surfaces as MaxRetryError, which must not replace the failure
# the snapshot is describing.
_PROBE_EXCEPTIONS: tuple[type[Exception], ...] = (
    ApiException,
    DynamicApiError,
    ProtocolError,
    MaxRetryError,
    AttributeError,
    KeyError,
    TypeError,
    ValueError,
    OSError,
    NotImplementedError,
)
# RuntimeError is not raised by this module: it is caught so a probe that fails that way records a
# failure instead of dying silently on its worker thread and leaving the snapshot looking healthy.
# mypy rejects a starred tuple inside an except clause, hence the explicit constant.
_PROBE_FAILURES: tuple[type[Exception], ...] = (*_PROBE_EXCEPTIONS, RuntimeError)


def _render_json(value: Any) -> str:
    """Render probe data as JSON: cluster objects are dynamic, provider-shaped data.

    Args:
        value (Any): Data a probe returned, usually plain dicts from ``to_dict()``.

    Returns:
        str: Indented JSON; leaves the serializer cannot type are stringified.
    """
    return json.dumps(value, indent=2, default=str)


def _render_lines(lines: list[str]) -> str:
    """Render a line summary as plain text, one line per record.

    Args:
        lines (list[str]): Summary lines built from provider data.

    Returns:
        str: The lines joined by newlines.
    """
    return "\n".join(lines)


def _truncate_output(text: str) -> str:
    """Cap one probe's output so a single probe cannot inflate the snapshot.

    Args:
        text (str): Rendered output of one probe.

    Returns:
        str: The text unchanged within the budget, otherwise its head plus a truncation marker
            naming how many bytes were dropped.
    """
    encoded = text.encode(_OUTPUT_ENCODING, errors="replace")
    if len(encoded) <= _MAX_PROBE_OUTPUT_BYTES:
        return text
    head = encoded[:_MAX_PROBE_OUTPUT_BYTES].decode(_OUTPUT_ENCODING, errors="replace")
    return f"{head}\n{_TRUNCATION_MARKER.format(omitted=len(encoded) - _MAX_PROBE_OUTPUT_BYTES)}"


def _call_within_budget(*, probe: Callable[[], Any], budget: float) -> tuple[Any, Exception | None]:
    """Run one probe with a hard wait limit, regardless of the client's own timeouts.

    The Kubernetes client only bounds a request when the caller passes ``_request_timeout``, which
    the ocp_resources wrappers never do, so a silent API server would hold the snapshot past its
    deadline and delay the failure being reported. The probe therefore runs on a daemon thread: the
    snapshot stops waiting when the budget expires, while the request finishes (or not) on its own.

    Args:
        probe (Callable[[], Any]): Read performed against the run client; its value is provider-shaped
            cluster data the wrapper exposes dynamically, so the type stays Any.
        budget (float): Seconds the snapshot may still spend on this probe.

    Returns:
        tuple[Any, Exception | None]: The probe value, or the exception it raised (including
            ClusterStateProbeTimeout), with None in the other slot. The value is Any because it is
            whatever the probe read from the cluster, rendered by the caller.

    Raises:
        RuntimeError: Never raised here; a probe that fails records its exception instead of
            propagating it, so the snapshot always renders a failure record.
    """
    # Any: the probe returns dynamic cluster data (a resource list or its instance dict) that has no
    # narrower static type, and the dict only carries the two outcomes of the thread below.
    outcome: dict[str, Any] = {}

    def _execute_probe() -> None:
        """Run the probe and record either its value or the exception it raised."""
        try:
            outcome["value"] = probe()
        except _PROBE_FAILURES as exc:
            # RuntimeError is not raised here: it is caught so a probe that fails that way records a
            # failure record instead of dying silently and leaving the snapshot looking healthy.
            outcome["failure"] = exc

    worker = threading.Thread(target=_execute_probe, name="cluster-state-probe", daemon=True)
    worker.start()
    worker.join(timeout=budget)
    if worker.is_alive():
        return None, ClusterStateProbeTimeout()
    return outcome.get("value"), outcome.get("failure")


@dataclass
class _Probes:
    """Probe runner of one snapshot: one run client, one total deadline, one skip counter.

    Args:
        client (DynamicClient): Run client every probe reads the cluster with.
        data_collector_path (Path | None): Artifact root; the configured root when None.
        subject (str | None): Plan name the snapshot belongs to, for the file name and header.
        started (float): ``monotonic()`` value the snapshot deadline is measured from.
        skipped (int): Probes not run because the snapshot deadline had already passed.
    """

    client: DynamicClient
    started: float = field(default_factory=monotonic)
    skipped: int = 0

    def run(self, label: str, probe: Callable[[], Any], *, render: Callable[[Any], str] = _render_json) -> str:
        """Run one probe inside the snapshot budget and render its record.

        Args:
            label (str): Readable probe label, written as the section header.
            probe (Callable[[], Any]): Read performed against the run client.
            render (Callable[[Any], str]): Renders the value the probe returned.

        Returns:
            str: The rendered record, or a skip/failure note when the probe did not produce data.
        """
        return self.fetch(label=label, probe=probe, render=render)[0]

    def fetch(
        self, label: str, probe: Callable[[], Any], *, render: Callable[[Any], str] = _render_json
    ) -> tuple[str, Any]:
        """Run one probe and also return the data behind its record, for probes that feed another.

        Args:
            label (str): Readable probe label, written as the section header.
            probe (Callable[[], Any]): Read performed against the run client.
            render (Callable[[Any], str]): Renders the value the probe returned.

        Returns:
            tuple[str, Any]: The record and the raw value, which is None when the probe was
                skipped or failed.
        """
        if self._remaining_seconds() <= 0:
            self.skipped += 1
            return (
                f"{_PROBE_LABEL_PREFIX}{label}\n[skipped: snapshot deadline of {_SNAPSHOT_DEADLINE_SECONDS}s reached]",
                None,
            )
        value, failure = _call_within_budget(probe=probe, budget=self._remaining_seconds())
        if failure is not None:
            if isinstance(failure, ClusterStateProbeTimeout):
                # The request itself cannot be cancelled: it keeps its connection until the socket
                # gives up, but the snapshot no longer waits for it and the original failure is
                # reported on time.
                LOGGER.warning(f"Cluster state probe '{label}' exceeded the snapshot budget")
                return (
                    f"{_PROBE_LABEL_PREFIX}{label}\n"
                    f"[probe exceeded the remaining snapshot budget of {_SNAPSHOT_DEADLINE_SECONDS}s]",
                    None,
                )
            LOGGER.warning(f"Cluster state probe '{label}' failed: {failure}")
            return f"{_PROBE_LABEL_PREFIX}{label}\n[probe failed: {type(failure).__name__}: {failure}]", None
        return self.record(label=label, value=value, render=render), value

    def record(self, label: str, value: Any, *, render: Callable[[Any], str] = _render_json) -> str:
        """Render data under a probe label, bounded by the per-probe output cap.

        Args:
            label (str): Readable probe label, written as the section header.
            value (Any): Data to render.
            render (Callable[[Any], str]): Renders the value.

        Returns:
            str: The rendered record.
        """
        return f"{_PROBE_LABEL_PREFIX}{label}\n{_truncate_output(text=render(value))}"

    def deadline_record(self) -> str | None:
        """Render the record that closes a snapshot cut short by its deadline.

        Returns:
            str | None: One summary line, or None when the whole snapshot fit in the budget.
        """
        if not self.skipped:
            return None
        elapsed = int(monotonic() - self.started)
        return (
            f"# snapshot deadline reached after {elapsed}s of {_SNAPSHOT_DEADLINE_SECONDS}s, "
            f"{self.skipped} probes skipped"
        )

    def _remaining_seconds(self) -> float:
        """Return the time left in the snapshot budget.

        Returns:
            float: Seconds remaining, or a value of zero or less once the deadline has passed.
        """
        return self.started + _SNAPSHOT_DEADLINE_SECONDS - monotonic()


def _wrapper(
    resource_cls: type[Resource], *, client: DynamicClient, name: str, namespace: str | None = None
) -> Resource:
    """Instantiate a wrapper class without reading anything from the cluster.

    Args:
        resource_cls (type[Resource]): Wrapper class of the kind.
        client (DynamicClient): Run client.
        name (str): Object name, or the placeholder name when only the kind's API handle is needed.
        namespace (str | None): Namespace of a namespaced kind.

    Returns:
        Resource: The wrapper instance.
    """
    if issubclass(resource_cls, NamespacedResource):
        return resource_cls(client=client, name=name, namespace=namespace)
    return resource_cls(client=client, name=name)


def _named_instance(
    resource_cls: type[Resource], *, client: DynamicClient, name: str, namespace: str | None = None
) -> dict[str, Any]:
    """Read one object's full body through its wrapper class.

    Args:
        resource_cls (type[Resource]): Wrapper class of the kind.
        client (DynamicClient): Run client.
        name (str): Object name.
        namespace (str | None): Namespace of a namespaced kind.

    Returns:
        dict[str, Any]: The object as a plain dict.
    """
    return _wrapper(resource_cls, client=client, name=name, namespace=namespace).instance.to_dict()


def _list_items(
    resource_cls: type[Resource], *, client: DynamicClient, namespace: str | None = None
) -> list[dict[str, Any]]:
    """Read one kind's listing in a single request.

    ``Resource.get()`` yields a wrapper per object and reading each object's ``instance`` would be
    one extra GET per object: too slow for hundreds of PVCs inside the snapshot deadline.

    Args:
        resource_cls (type[Resource]): Wrapper class of the kind; it resolves the API version.
        client (DynamicClient): Run client.
        namespace (str | None): Namespace of a namespaced kind; None lists every namespace.

    Returns:
        list[dict[str, Any]]: The listed objects as plain dicts, empty when the kind has none.
    """
    api = _wrapper(resource_cls, client=client, name=_KIND_HANDLE_NAME, namespace=namespace).api
    listing = api.get(namespace=namespace) if namespace else api.get()
    return [item.to_dict() for item in listing.items]


def _resource_name(instance: dict[str, Any]) -> str:
    """Return an object's name for the one-line summaries below.

    Args:
        instance (dict[str, Any]): Object body.

    Returns:
        str: The name, or a placeholder for an object the API returned without one.
    """
    return (instance.get("metadata") or {}).get("name") or "<unnamed>"


def _conditions(*, instance: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index one object's status conditions by their type.

    Args:
        instance (dict[str, Any]): Object body.

    Returns:
        dict[str, dict[str, Any]]: Condition per type, empty when the object reports none.
    """
    return {
        condition.get("type", ""): condition for condition in (instance.get("status") or {}).get("conditions") or []
    }


def _condition_status(*, conditions: dict[str, dict[str, Any]], condition_type: str) -> str:
    """Return one condition's status.

    Args:
        conditions (dict[str, dict[str, Any]]): Conditions indexed by type.
        condition_type (str): Condition type to read.

    Returns:
        str: The reported status, or ``<missing>`` when the object never reported that condition.
    """
    return conditions.get(condition_type, {}).get("status") or "<missing>"


def _cluster_operator_summary(operators: list[dict[str, Any]]) -> list[str]:
    """Render each ClusterOperator's availability and current version, like ``oc get co``.

    Args:
        operators (list[dict[str, Any]]): ClusterOperator bodies.

    Returns:
        list[str]: One line per operator.
    """
    lines: list[str] = []
    for operator in operators:
        conditions = _conditions(instance=operator)
        status = operator.get("status") or {}
        versions = ",".join(version.get("version", "") for version in status.get("versions") or [])
        lines.append(
            f"{_resource_name(operator)}: "
            f"available={_condition_status(conditions=conditions, condition_type='Available')} "
            f"progressing={_condition_status(conditions=conditions, condition_type='Progressing')} "
            f"degraded={_condition_status(conditions=conditions, condition_type='Degraded')} "
            f"version={versions}"
        )
    return lines


def _machine_config_pool_summary(pools: list[dict[str, Any]]) -> list[str]:
    """Render the counters and conditions of every MachineConfigPool.

    Args:
        pools (list[dict[str, Any]]): MachineConfigPool bodies.

    Returns:
        list[str]: One line per pool, followed by one line per condition.
    """
    lines: list[str] = []
    for pool in pools:
        status = pool.get("status") or {}
        lines.append(
            f"{_resource_name(pool)}: ready={status.get('readyMachineCount')} "
            f"updated={status.get('updatedMachineCount')} degraded={status.get('degradedMachineCount')} "
            f"unavailable={status.get('unavailableMachineCount')}"
        )
        for condition in status.get("conditions") or []:
            lines.append(
                f"  {condition.get('type')}={condition.get('status')} "
                f"reason={condition.get('reason')} message={condition.get('message')}"
            )
    return lines


def _node_summary(nodes: list[dict[str, Any]]) -> list[str]:
    """Render each node's readiness, roles and kubelet version, like ``oc get nodes -o wide``.

    Args:
        nodes (list[dict[str, Any]]): Node bodies.

    Returns:
        list[str]: One line per node.
    """
    lines: list[str] = []
    for node in nodes:
        conditions = _conditions(instance=node)
        labels = (node.get("metadata") or {}).get("labels") or {}
        roles = sorted(
            label.removeprefix(_NODE_ROLE_LABEL_PREFIX) for label in labels if label.startswith(_NODE_ROLE_LABEL_PREFIX)
        )
        node_info = (node.get("status") or {}).get("nodeInfo") or {}
        lines.append(
            f"{_resource_name(node)}: Ready={_condition_status(conditions=conditions, condition_type='Ready')} "
            f"unschedulable={bool((node.get('spec') or {}).get('unschedulable'))} "
            f"roles={','.join(roles) or 'none'} version={node_info.get('kubeletVersion')}"
        )
    return lines


def _pod_summary(pods: list[dict[str, Any]]) -> list[str]:
    """Render each pod's phase, readiness, restarts and node, like ``oc get pods -o wide``.

    Args:
        pods (list[dict[str, Any]]): Pod bodies.

    Returns:
        list[str]: One line per pod.
    """
    lines: list[str] = []
    for pod in pods:
        status = pod.get("status") or {}
        container_statuses = status.get("containerStatuses") or []
        ready = sum(1 for container_status in container_statuses if container_status.get("ready"))
        restarts = sum(container_status.get("restartCount") or 0 for container_status in container_statuses)
        lines.append(
            f"{_resource_name(pod)}: phase={status.get('phase')} ready={ready}/{len(container_statuses)} "
            f"restarts={restarts} node={(pod.get('spec') or {}).get('nodeName')}"
        )
    return lines


def _persistent_volume_claim_summary(claims: list[dict[str, Any]]) -> list[str]:
    """Render each PVC's phase, bound volume, capacity and storage class.

    Args:
        claims (list[dict[str, Any]]): PersistentVolumeClaim bodies.

    Returns:
        list[str]: One line per PVC.
    """
    lines: list[str] = []
    for claim in claims:
        status = claim.get("status") or {}
        spec = claim.get("spec") or {}
        lines.append(
            f"{_resource_name(claim)}: phase={status.get('phase')} volume={spec.get('volumeName')} "
            f"capacity={(status.get('capacity') or {}).get('storage')} "
            f"storageClass={spec.get('storageClassName')}"
        )
    return lines


def _data_volume_summary(data_volumes: list[dict[str, Any]]) -> list[str]:
    """Render each DataVolume's phase, progress and PVC.

    Args:
        data_volumes (list[dict[str, Any]]): DataVolume bodies.

    Returns:
        list[str]: One line per DataVolume.
    """
    lines: list[str] = []
    for data_volume in data_volumes:
        status = data_volume.get("status") or {}
        lines.append(
            f"{_resource_name(data_volume)}: phase={status.get('phase')} progress={status.get('progress')} "
            f"pvc={status.get('claimName')}"
        )
    return lines


def _persistent_volume_summary(volumes: list[dict[str, Any]]) -> list[str]:
    """Render each PersistentVolume's phase, claim, capacity and storage class.

    Args:
        volumes (list[dict[str, Any]]): PersistentVolume bodies.

    Returns:
        list[str]: One line per volume.
    """
    lines: list[str] = []
    for volume in volumes:
        spec = volume.get("spec") or {}
        status = volume.get("status") or {}
        claim = spec.get("claimRef") or {}
        lines.append(
            f"{_resource_name(volume)}: phase={status.get('phase')} "
            f"claim={claim.get('namespace')}/{claim.get('name')} "
            f"capacity={(spec.get('capacity') or {}).get('storage')} storageClass={spec.get('storageClassName')} "
            f"reason={status.get('reason')}"
        )
    return lines


def _storage_class_summary(storage_classes: list[dict[str, Any]]) -> list[str]:
    """Render each StorageClass' provisioner and binding behaviour, like ``oc get sc -o wide``.

    Args:
        storage_classes (list[dict[str, Any]]): StorageClass bodies.

    Returns:
        list[str]: One line per storage class.
    """
    return [
        f"{_resource_name(storage_class)}: provisioner={storage_class.get('provisioner')} "
        f"reclaimPolicy={storage_class.get('reclaimPolicy')} "
        f"volumeBindingMode={storage_class.get('volumeBindingMode')} "
        f"allowVolumeExpansion={storage_class.get('allowVolumeExpansion')}"
        for storage_class in storage_classes
    ]


def _cluster_service_version_summary(versions: list[dict[str, Any]]) -> list[str]:
    """Render each ClusterServiceVersion's phase and version, like ``oc get csv``.

    Args:
        versions (list[dict[str, Any]]): ClusterServiceVersion bodies.

    Returns:
        list[str]: One line per CSV.
    """
    return [
        f"{_resource_name(version)}: phase={(version.get('status') or {}).get('phase')} "
        f"version={(version.get('spec') or {}).get('version')}"
        for version in versions
    ]


def _event_summary(events: list[dict[str, Any]]) -> list[str]:
    """Render each event as one timeline line: when, what, and which object.

    Args:
        events (list[dict[str, Any]]): Event bodies in the order they happened.

    Returns:
        list[str]: One line per event.
    """
    lines: list[str] = []
    for event in events:
        involved = event.get("involvedObject") or {}
        lines.append(
            f"{event.get('lastTimestamp') or event.get('eventTime')} {event.get('type')} {event.get('reason')} "
            f"{involved.get('kind')}/{involved.get('name')}: {event.get('message')} (count={event.get('count')})"
        )
    return lines


def _namespace_events(*, client: DynamicClient, namespace: str) -> list[str]:
    """Read a namespace's recent events oldest first, so the snapshot reads like a timeline.

    Args:
        client (DynamicClient): Run client.
        namespace (str): Namespace holding the events.

    Returns:
        list[str]: One line per event, oldest first.
    """
    return _event_summary(
        events=[
            event.to_dict()
            for event in reversed(Event.list(client=client, namespace=namespace, since_seconds=_EVENT_LOOKBACK_SECONDS))
        ]
    )


def _cluster_image(*, client: DynamicClient) -> dict[str, Any]:
    """Read the cluster-wide image configuration: its digest mirrors and blocked registries.

    Args:
        client (DynamicClient): Run client.

    Returns:
        dict[str, Any]: The ``image.config.openshift.io`` cluster object as a plain dict.
    """
    image_api = client.resources.get(api_version=_CLUSTER_IMAGE_API_VERSION, kind=_CLUSTER_IMAGE_KIND)
    return image_api.get(name=_CLUSTER_IMAGE_NAME).to_dict()


def _listing_sections(
    *,
    probes: _Probes,
    label: str,
    resource_cls: type[Resource],
    namespace: str | None = None,
    summarise: Callable[[list[dict[str, Any]]], list[str]] | None = None,
    include_listing: bool = True,
) -> list[str]:
    """Collect one kind's listing, followed by a line summary of it when a summariser is given.

    Args:
        probes (_Probes): Probe runner of the current snapshot.
        label (str): Readable label of the listing.
        resource_cls (type[Resource]): Wrapper class of the kind.
        namespace (str | None): Namespace of a namespaced kind.
        summarise (Callable[[list[dict[str, Any]]], list[str]] | None): Builds readable lines
            from the listing, so the artifact never needs the full objects to answer "what state?".
        include_listing (bool): Write the full objects too; off for kinds whose bodies are large
            enough to fill the snapshot on their own (nodes, CSVs) and whose summary carries the
            state a failure needs.

    Returns:
        list[str]: The listing record when requested, plus the summary record when one was requested.
    """
    record, items = probes.fetch(
        label=label, probe=partial(_list_items, resource_cls, client=probes.client, namespace=namespace)
    )
    sections = [record] if include_listing or summarise is None or items is None else []
    if items is not None and summarise is not None:
        sections.append(probes.record(label=f"{label} summary", value=summarise(items), render=_render_lines))
    return sections


def _previous_container_log(*, pod: Pod, container: str, tail_lines: int) -> str:
    """Read a container's previous log, or explain that the instance is gone.

    A restarted container's previous instance can already be garbage-collected, and the API answers
    400 "previous terminated container ... not found". That is expected, not a failed probe.

    Args:
        pod (Pod): Pod wrapper to read from.
        container (str): Container name.
        tail_lines (int): Lines kept.

    Returns:
        str: The previous log, or a note when no previous instance is retained.

    Raises:
        ApiException: For any other rejection of the log request.
    """
    try:
        return pod.log(container=container, tail_lines=tail_lines, previous=True)
    except ApiException as exc:
        if exc.status == _HTTP_BAD_REQUEST and "previous terminated container" in str(exc):
            return f"[no previous instance retained for container '{container}']"
        raise


def _restarted_containers(*, pod: dict[str, Any]) -> set[str]:
    """Return the containers that have a previous instance to read.

    Asking for a previous log of a container that never restarted makes the API answer 400, which
    would be recorded as a probe failure and make a healthy snapshot look broken.

    Args:
        pod (dict[str, Any]): Pod body, whose status carries one containerStatus per container.

    Returns:
        set[str]: Names of the containers whose restart count is above zero.
    """
    statuses = (pod.get("status") or {}).get("containerStatuses") or []
    return {
        status["name"]
        for status in statuses
        if isinstance(status, dict) and status.get("name") and (status.get("restartCount") or 0) > 0
    }


def _pod_log_sections(
    *, probes: _Probes, label: str, namespace: str, pod_prefixes: tuple[str, ...], tail_lines: int
) -> list[str]:
    """Collect the pods of a namespace and every container log of the matching ones.

    Each container is read twice: its current log holds the errors produced at dump time even when
    the container restarted, and its previous log is added when the container has one. A container
    that never restarted makes the API reject the previous-log read, which is recorded as a note.

    Args:
        probes (_Probes): Probe runner of the current snapshot.
        label (str): Readable label of the pod listing.
        namespace (str): Namespace holding the pods.
        pod_prefixes (tuple[str, ...]): Pod name prefixes whose logs are worth the artifact space.
        tail_lines (int): Lines kept per container log.

    Returns:
        list[str]: The pod listing, its summary, and one record per container log.
    """
    record, pods = probes.fetch(label=label, probe=partial(_list_items, Pod, client=probes.client, namespace=namespace))
    if pods is None:
        return [record]

    # The summary already carries phase, readiness, restarts and node: the full pod bodies are the
    # bulk of the artifact and add nothing the logs below do not show.
    del record
    sections = [probes.record(label=f"{label} summary", value=_pod_summary(pods=pods), render=_render_lines)]
    for pod in pods:
        pod_name = _resource_name(pod)
        if not pod_name.startswith(pod_prefixes):
            continue
        pod_wrapper = Pod(client=probes.client, name=pod_name, namespace=namespace)
        restarted_containers = _restarted_containers(pod=pod)
        for container in (container.get("name") for container in (pod.get("spec") or {}).get("containers") or []):
            sections.append(
                probes.run(
                    f"log {pod_name}/{container}",
                    partial(pod_wrapper.log, container=container, tail_lines=tail_lines),
                    render=str,
                )
            )
            if container in restarted_containers:
                sections.append(
                    probes.run(
                        f"log {pod_name}/{container} (previous)",
                        partial(_previous_container_log, pod=pod_wrapper, container=container, tail_lines=tail_lines),
                        render=str,
                    )
                )
    return sections


def _pending_pvc_sections(*, probes: _Probes, namespace: str) -> list[str]:
    """Collect the namespace PVCs, their summary, and the full body of the non-Bound ones.

    Args:
        probes (_Probes): Probe runner of the current snapshot.
        namespace (str): Namespace holding the PVCs.

    Returns:
        list[str]: The PVC summary, and one full-body record per non-Bound PVC.
    """
    label = f"PVCs in {namespace}"
    record, claims = probes.fetch(
        label=label, probe=partial(_list_items, PersistentVolumeClaim, client=probes.client, namespace=namespace)
    )
    if claims is None:
        return [record]

    sections = [
        probes.record(label=f"{label} summary", value=_persistent_volume_claim_summary(claims), render=_render_lines)
    ]
    pending = [claim for claim in claims if (claim.get("status") or {}).get("phase") != "Bound"]
    if len(pending) > _MAX_PENDING_PVC_DUMPS:
        sections.append(
            f"{_PROBE_LABEL_PREFIX}{label} full bodies\n"
            f"[only the first {_MAX_PENDING_PVC_DUMPS} of {len(pending)} non-Bound PVCs are written]"
        )
    sections.extend(
        probes.run(
            f"PVC {_resource_name(claim)}",
            partial(
                _named_instance,
                PersistentVolumeClaim,
                client=probes.client,
                name=_resource_name(claim),
                namespace=namespace,
            ),
        )
        for claim in pending[:_MAX_PENDING_PVC_DUMPS]
    )
    return sections


def _dump_upgrade_scope(probes: _Probes) -> list[str]:
    """Collect the MachineConfig, operator and image state of an upgrade failure.

    Args:
        probes (_Probes): Probe runner of the current snapshot.

    Returns:
        list[str]: One rendered record per probe.
    """
    sections = [
        probes.run(
            "ClusterVersion",
            partial(_named_instance, ClusterVersion, client=probes.client, name=_CLUSTER_VERSION_NAME),
        ),
        *_listing_sections(
            probes=probes,
            label="ClusterOperators",
            resource_cls=ClusterOperator,
            summarise=_cluster_operator_summary,
            include_listing=False,
        ),
        probes.run(
            "ClusterOperator machine-config",
            partial(_named_instance, ClusterOperator, client=probes.client, name=_MACHINE_CONFIG_OPERATOR),
        ),
        *_listing_sections(
            probes=probes,
            label="MachineConfigPools",
            resource_cls=MachineConfigPool,
            summarise=_machine_config_pool_summary,
            include_listing=False,
        ),
        *_listing_sections(
            probes=probes, label="Nodes", resource_cls=Node, summarise=_node_summary, include_listing=False
        ),
        *_pod_log_sections(
            probes=probes,
            label=f"Pods in {_MCO_NAMESPACE}",
            namespace=_MCO_NAMESPACE,
            pod_prefixes=_MCO_POD_PREFIXES,
            tail_lines=_MCO_LOG_TAIL_LINES,
        ),
    ]
    sections.extend(_listing_sections(probes=probes, label="IDMS", resource_cls=ImageDigestMirrorSet))
    sections.extend(_listing_sections(probes=probes, label="ICSP", resource_cls=ImageContentSourcePolicy))
    sections.append(probes.run("Image cluster", partial(_cluster_image, client=probes.client)))
    sections.extend(
        _listing_sections(
            probes=probes,
            label="MTV CSVs",
            resource_cls=ClusterServiceVersion,
            namespace=_MTV_NAMESPACE,
            summarise=_cluster_service_version_summary,
            include_listing=False,
        )
    )
    return sections


def _dump_storage_scope(probes: _Probes, *, namespace: str) -> list[str]:
    """Collect PVC, DataVolume, event and ceph-csi provisioner state of a stalled migration.

    Args:
        probes (_Probes): Probe runner of the current snapshot.
        namespace (str): Migration target namespace.

    Returns:
        list[str]: One rendered record per probe.
    """
    sections = _pending_pvc_sections(probes=probes, namespace=namespace)
    sections.extend(
        _listing_sections(
            probes=probes,
            label=f"DataVolumes in {namespace}",
            resource_cls=DataVolume,
            namespace=namespace,
            summarise=_data_volume_summary,
            include_listing=False,
        )
    )
    sections.extend(
        _listing_sections(
            probes=probes,
            label="PersistentVolumes",
            resource_cls=PersistentVolume,
            summarise=_persistent_volume_summary,
            include_listing=False,
        )
    )
    sections.append(
        probes.run(
            "Events",
            partial(_namespace_events, client=probes.client, namespace=namespace),
            render=_render_lines,
        )
    )
    sections.extend(
        _listing_sections(
            probes=probes,
            label="StorageClasses",
            resource_cls=StorageClass,
            summarise=_storage_class_summary,
            include_listing=False,
        )
    )
    sections.extend(
        _pod_log_sections(
            probes=probes,
            label=f"Pods in {_STORAGE_NAMESPACE}",
            namespace=_STORAGE_NAMESPACE,
            pod_prefixes=_CSI_POD_PREFIXES,
            tail_lines=_CSI_LOG_TAIL_LINES,
        )
    )
    return sections


def _with_deadline_record(*, sections: list[str], probes: _Probes | None) -> list[str]:
    """Close a set of records with the truncation note of a snapshot cut short by its deadline.

    Args:
        sections (list[str]): Rendered records collected so far.
        probes (_Probes | None): Probe runner whose budget bounded the collection, or None when the
            records came from local data only.

    Returns:
        list[str]: The same records, plus one summary line when probes were skipped.
    """
    if probes is None:
        return sections
    deadline_record = probes.deadline_record()
    if deadline_record is not None:
        sections.append(deadline_record)
    return sections


def _reason_slug(reason: str) -> str:
    """Convert a failure reason to a file-name slug.

    Args:
        reason (str): Failure reason as written in the snapshot header.

    Returns:
        str: Lower-case, dash-separated, length-bounded slug.
    """
    slug = re.sub(r"[^a-z0-9]+", _REASON_SLUG_SEPARATOR, reason.lower()).strip(_REASON_SLUG_SEPARATOR)
    return (slug or _UNSPECIFIED_REASON)[:_REASON_SLUG_MAX_LENGTH]


def _worker_suffix() -> str:
    """Return the pytest-xdist worker id as a file-name suffix.

    Returns:
        str: ``-<worker id>`` in a parallel run, an empty string otherwise.
    """
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    return f"-{worker}" if worker else ""


def _snapshot_name(*, scope: str, reason: str, namespace: str | None, subject: str | None) -> str:
    """Build a self-describing snapshot file name.

    Analysis starts from the artifact tree, so the name carries what a reviewer needs to know which
    test produced it without opening the file: time, scope, reason, and the plan and namespace the
    failure belongs to.

    Args:
        scope (str): Snapshot scope (``upgrade`` or ``storage``).
        reason (str): Failure reason.
        namespace (str | None): Migration target namespace, when the scope has one.
        subject (str | None): Plan name the snapshot belongs to, when the scope has one.

    Returns:
        str: File name of the snapshot.
    """
    parts = [utc_timestamp(), scope, _reason_slug(reason)]
    if subject:
        parts.append(_reason_slug(subject))
    if namespace:
        parts.append(_reason_slug(namespace))
    return "-".join(parts) + _worker_suffix() + ".txt"


def _write_snapshot(
    *,
    collector_path: Path,
    scope: str,
    reason: str,
    sections: list[str],
    namespace: str | None = None,
    subject: str | None = None,
) -> Path:
    """Write one snapshot file.

    Args:
        collector_path (Path): Data-collector root directory.
        scope (str): Snapshot scope, used in the file name and header.
        reason (str): Failure reason, used in the file name and header.
        sections (list[str]): Rendered probe records, written in order.
        namespace (str | None): Migration target namespace, recorded when known.
        subject (str | None): Plan name the snapshot belongs to, recorded when known.

    Returns:
        Path: Path of the written snapshot.
    """
    directory = collector_path / _SNAPSHOT_DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    snapshot_path = directory / _snapshot_name(scope=scope, reason=reason, namespace=namespace, subject=subject)
    collected = utc_timestamp()
    header = (
        f"# reason: {reason}\n# scope: {scope}\n"
        f"# plan: {subject or 'n/a'}\n# namespace: {namespace or 'n/a'}\n"
        f"# client: run DynamicClient through ocp_resources wrappers\n# collected: {collected}"
    )
    text = f"{header}\n\n{_SECTION_SEPARATOR.join(sections)}\n"
    # A probed object can echo the cluster password; the artifact is not trusted to be safe raw.
    cluster_password = resolve_cluster_credential("cluster_password")
    if cluster_password:
        text = redact_secret(text=text, secret=cluster_password)
    snapshot_path.write_text(text, encoding=_OUTPUT_ENCODING)
    return snapshot_path


def _collect_and_write(
    *,
    collector_path: Path,
    reason: str,
    scope: str,
    collect: Callable[[_Probes], list[str]],
    client: DynamicClient,
    namespace: str | None = None,
    subject: str | None = None,
) -> None:
    """Write one snapshot for a scope and never raise.

    Args:
        collector_path (Path): Data-collector root directory.
        reason (str): Failure reason, used in the file name and header.
        scope (str): Snapshot scope, used in the file name and header.
        collect (Callable[[_Probes], list[str]]): Probe collector of the scope.
        client (DynamicClient): Run client every probe reads the cluster with.
        namespace (str | None): Migration target namespace, recorded when known.
        subject (str | None): Plan name the snapshot belongs to, recorded when known.
    """
    probes = _Probes(client=client)
    try:
        # A snapshot cut short by its deadline says so, so a short file is never mistaken for a
        # healthy cluster that simply had nothing to report.
        snapshot_path = _write_snapshot(
            collector_path=collector_path,
            scope=scope,
            reason=reason,
            sections=_with_deadline_record(sections=collect(probes), probes=probes),
            namespace=namespace,
            subject=subject,
        )
    except (AttributeError, KeyError, OSError, TypeError, ValueError) as exc:
        LOGGER.warning(f"Cluster state snapshot for scope '{scope}' (reason '{reason}') failed: {exc}")
        return

    LOGGER.warning(f"Cluster state snapshot written to {snapshot_path}")


def dump_cluster_state(
    *,
    reason: str,
    scope: Literal["upgrade", "storage"],
    namespace: str | None = None,
    client: DynamicClient,
    data_collector_path: Path | None = None,
    subject: str | None = None,
) -> None:
    """Write a bounded cluster-state snapshot for a real failure.

    Called immediately before a failure is raised, so the original exception stays the error the
    caller sees: collection is best effort, every probe is bounded, and nothing escapes.

    Args:
        reason (str): Short cause, used as the file-name slug and the file header.
        scope (Literal["upgrade", "storage"]): Which snapshot to collect.
        namespace (str | None): Migration target namespace, required by the storage scope.
        client (DynamicClient): Run client every probe reads the cluster with.
        data_collector_path (Path | None): Artifact root; the configured root when None.

    Raises:
        ValueError: If the storage scope is requested without a namespace.
    """
    collect: Callable[[_Probes], list[str]]
    if scope == "storage":
        if namespace is None:
            raise ValueError("dump_cluster_state with scope='storage' requires the migration target namespace")
        collect = partial(_dump_storage_scope, namespace=namespace)
    else:
        collect = _dump_upgrade_scope

    # Resolved before any probe, so skip mode costs no API request and no clock.
    collector_path = data_collector_path or shared_data_collector_path()
    if collector_path is None:
        return

    _collect_and_write(
        collector_path=collector_path,
        reason=reason,
        scope=scope,
        collect=collect,
        client=client,
        namespace=namespace,
        subject=subject,
    )


def _session_start_cluster_sections(probes: _Probes) -> list[str]:
    """Collect the pre-run cluster state used as the run baseline.

    Args:
        probes (_Probes): Probe runner of the current snapshot.

    Returns:
        list[str]: One rendered record per probe.
    """
    sections = [
        probes.run(
            "ClusterVersion",
            partial(_named_instance, ClusterVersion, client=probes.client, name=_CLUSTER_VERSION_NAME),
        ),
        *_listing_sections(
            probes=probes,
            label="ClusterOperators",
            resource_cls=ClusterOperator,
            summarise=_cluster_operator_summary,
            include_listing=False,
        ),
        *_listing_sections(
            probes=probes,
            label="MachineConfigPools",
            resource_cls=MachineConfigPool,
            summarise=_machine_config_pool_summary,
            include_listing=False,
        ),
        *_listing_sections(
            probes=probes, label="Nodes", resource_cls=Node, summarise=_node_summary, include_listing=False
        ),
    ]
    sections.extend(
        _listing_sections(
            probes=probes,
            label="MTV CSVs",
            resource_cls=ClusterServiceVersion,
            namespace=_MTV_NAMESPACE,
            summarise=_cluster_service_version_summary,
            include_listing=False,
        )
    )
    sections.extend(
        _listing_sections(
            probes=probes,
            label="CNV CSVs",
            resource_cls=ClusterServiceVersion,
            namespace=_CNV_NAMESPACE,
            summarise=_cluster_service_version_summary,
            include_listing=False,
        )
    )
    return sections


def _git_directory() -> Path:
    """Return the git directory of the checkout this module is imported from.

    Returns:
        Path: The git directory; a worktree keeps it outside the working tree and names it in a
            ``.git`` file.
    """
    git_path = Path(__file__).resolve().parents[1] / ".git"
    if git_path.is_dir():
        return git_path
    # A worktree's .git is a file holding "gitdir: <path>".
    return Path(git_path.read_text(encoding=_OUTPUT_ENCODING).split(":", 1)[1].strip())


def _git_common_directory(git_dir: Path) -> Path:
    """Return the directory holding the refs of a git directory.

    Args:
        git_dir (Path): Git directory of the checkout.

    Returns:
        Path: The shared git directory a worktree points at, or ``git_dir`` when there is none.
    """
    commondir = git_dir / "commondir"
    if not commondir.exists():
        return git_dir
    return (git_dir / commondir.read_text(encoding=_OUTPUT_ENCODING).strip()).resolve()


def _git_head() -> str:
    """Read the checkout's revision from git's own files, without starting a subprocess.

    Returns:
        str: The current revision, or ``unknown`` when the checkout has no readable git metadata.
    """
    try:
        git_dir = _git_directory()
        head = (git_dir / "HEAD").read_text(encoding=_OUTPUT_ENCODING).strip()
        if not head.startswith(_GIT_REF_PREFIX):
            return head
        # HEAD of a worktree holds only the ref name; the ref itself lives in the common directory
        # and may be loose or packed (a pack-refs run removes the loose file this used to need).
        ref = head.removeprefix(_GIT_REF_PREFIX)
        for directory in (git_dir, _git_common_directory(git_dir=git_dir)):
            ref_file = directory / ref
            if ref_file.exists():
                return ref_file.read_text(encoding=_OUTPUT_ENCODING).strip()
            packed_sha = _packed_ref_sha(packed_refs=directory / _GIT_PACKED_REFS_NAME, ref=ref)
            if packed_sha:
                return packed_sha
        LOGGER.warning(f"Could not resolve the git ref '{ref}', recording {_GIT_HEAD_FALLBACK}")
    except (OSError, ValueError) as exc:
        LOGGER.warning(f"Could not read the checkout revision, recording {_GIT_HEAD_FALLBACK}: {exc}")
    return _GIT_HEAD_FALLBACK


def _packed_ref_sha(*, packed_refs: Path, ref: str) -> str | None:
    """Find a branch revision in a repository's packed refs file.

    Args:
        packed_refs (Path): Path to ``packed-refs`` in the git directory.
        ref (str): Full ref name, e.g. ``refs/heads/main``.

    Returns:
        str | None: The packed revision, or None when the file or the ref is absent.
    """
    if not packed_refs.exists():
        return None
    for line in packed_refs.read_text(encoding=_OUTPUT_ENCODING).splitlines():
        if line.startswith("#") or not line.strip():
            continue
        sha, _, packed_name = line.partition(" ")
        if packed_name.strip() == ref:
            return sha.strip()
    return None


def _redact_invocation(pytest_args: Sequence[str]) -> str:
    """Render the pytest invocation with every credential removed.

    Args:
        pytest_args (Sequence[str]): Arguments pytest was invoked with.

    Returns:
        str: The invocation with credential ``--tc`` values and URL userinfo replaced by ``***``.
    """
    redacted: list[str] = []
    value_follows_flag = False
    for arg in pytest_args:
        redacted_arg = _redact_argument(arg=arg)
        # pytest accepts ``--tc key:value`` as two argv entries, so the value is only recognized
        # through the argument before it.
        if value_follows_flag:
            redacted_arg = _redact_tc_value(value=redacted_arg)
        value_follows_flag = arg == _TC_ARGUMENT_FLAG
        redacted.append(redacted_arg)

    command_line = " ".join(redacted)
    # The credential may come from CLUSTER_PASSWORD alone, so it can appear outside any --tc value;
    # a missing password leaves the text as is instead of aborting the whole baseline.
    return redact_secret(text=command_line, secret=resolve_cluster_credential("cluster_password") or "")


def _redact_tc_value(value: str) -> str:
    """Redact a credential ``--tc`` value of the ``key:value`` form.

    Args:
        value (str): ``--tc`` value, or the argument that follows a bare ``--tc``.

    Returns:
        str: The value with a credential-ish key's value replaced by ``***``.
    """
    key, separator, _ = value.partition(_TC_KEY_SEPARATOR)
    if separator and _CREDENTIAL_KEY_PATTERN.search(key):
        return f"{key}{_TC_KEY_SEPARATOR}{_REDACTED_VALUE}"
    return value


def _redact_argument(arg: str) -> str:
    """Redact the secrets one invocation argument can carry.

    Args:
        arg (str): One pytest argument, as given on the command line.

    Returns:
        str: The argument with a credential ``--tc`` value and any URL userinfo redacted.
    """
    if arg.startswith(_TC_ARGUMENT_PREFIX):
        arg = f"{_TC_ARGUMENT_PREFIX}{_redact_tc_value(value=arg[len(_TC_ARGUMENT_PREFIX) :])}"
    return _URL_USERINFO_PATTERN.sub(rf"\g<scheme>{_REDACTED_VALUE}@", arg)


def _session_start_identity_sections(
    *, probes: _Probes | None, pytest_args: Sequence[str], skip_teardown: bool, skip_data_collector: bool
) -> list[str]:
    """Collect the run identity: checkout revision, pytest invocation and skip flags.

    The identity is local data, so it is collected even when no client could be built - only the
    cluster-dependent part (the authenticated user) is then omitted.

    Args:
        probes (_Probes | None): Probe runner of the current snapshot, or None without a client.
        pytest_args (Sequence[str]): Arguments pytest was invoked with, including ``--tc`` overrides.
        skip_teardown (bool): Whether ``--skip-teardown`` was set.
        skip_data_collector (bool): Whether ``--skip-data-collector`` was set.

    Returns:
        list[str]: One rendered record per identity probe.
    """
    sections = [
        f"{_PROBE_LABEL_PREFIX}git HEAD\n{_git_head()}",
        f"{_PROBE_LABEL_PREFIX}pytest\n{_redact_invocation(pytest_args=pytest_args)}",
    ]
    if probes is not None:
        sections.append(
            probes.run("current user", partial(_named_instance, User, client=probes.client, name=_CURRENT_USER_NAME))
        )
    else:
        sections.append(f"{_PROBE_LABEL_PREFIX}current user\n[not collected: no cluster client]")
    sections.append(f"# skip_teardown: {skip_teardown}\n# skip_data_collector: {skip_data_collector}")
    return sections


def _write_once_atomic(path: Path, text: str) -> bool:
    """Publish a complete file only when the destination does not exist yet.

    Every xdist worker writes the same session-start files. The content is written to a
    process-unique temporary sibling and then published - by hard link when the filesystem allows
    it, otherwise by an exclusive create - so the file appears complete and the first writer wins
    instead of a later worker overwriting the true start-of-run snapshot.

    Args:
        path (Path): Destination file.
        text (str): Content to write.

    Returns:
        bool: True when this call created the file, False when a snapshot already existed.
    """
    temporary_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary_path.write_text(text, encoding=_OUTPUT_ENCODING)
    try:
        os.link(temporary_path, path)
    except FileExistsError:
        return False
    except OSError:
        # Some artifact mounts reject hard links (EPERM/EXDEV/EOPNOTSUPP); an exclusive create is
        # equally atomic and keeps the first-writer-wins behaviour instead of losing the baseline.
        try:
            with path.open("x", encoding=_OUTPUT_ENCODING) as handle:
                handle.write(text)
        except FileExistsError:
            return False
    finally:
        temporary_path.unlink(missing_ok=True)
    return True


def _session_client() -> DynamicClient | None:
    """Return a client for the baseline probes, or None when the run has none.

    The baseline is a diagnostic: a run without usable credentials skips its cluster section
    instead of failing the session over a missing snapshot.

    Returns:
        DynamicClient | None: Client built from the resolved credentials, or None when building it failed.
    """
    try:
        with _bounded_requests():
            return get_cluster_client()
    except (OSError, ValueError, TypeError, ApiException, DynamicApiError, ClientWithBasicAuthError) as exc:
        # A rejected credential is a configuration problem elsewhere in the run: the baseline is a
        # diagnostic, so it degrades to skipping its cluster section instead of raising into pytest.
        LOGGER.warning(f"Session-start baseline has no cluster client, skipping its cluster state: {exc}")
        return None


def _redact_cluster_password(text: str) -> str:
    """Redact the configured cluster password from artifact text.

    Args:
        text (str): Text about to be written into an artifact.

    Returns:
        str: The text with the password replaced, unchanged when no password is configured.
    """
    cluster_password = resolve_cluster_credential("cluster_password")
    return redact_secret(text=text, secret=cluster_password) if cluster_password else text


def _write_session_cluster_snapshot(*, run_client: DynamicClient, path: Path) -> bool:
    """Collect and write the session-start cluster state.

    Args:
        run_client (DynamicClient | None): Client the probes read the cluster with, or None to write
            the local identity only.
        path (Path): Destination file.

    Returns:
        bool: True when this call created the file, False when a snapshot already existed.
    """
    probes = _Probes(client=run_client)
    sections = _with_deadline_record(sections=_session_start_cluster_sections(probes=probes), probes=probes)
    return _write_once_atomic(path=path, text=_redact_cluster_password(f"{_SECTION_SEPARATOR.join(sections)}\n"))


def _write_session_identity(
    *,
    run_client: DynamicClient | None,
    path: Path,
    pytest_args: Sequence[str],
    skip_teardown: bool,
    skip_data_collector: bool,
) -> bool:
    """Collect and write the run identity of this session.

    Args:
        run_client (DynamicClient): Client the probes read the cluster with.
        path (Path): Destination file.
        pytest_args (Sequence[str]): Arguments pytest was invoked with, including ``--tc`` overrides.
        skip_teardown (bool): Whether ``--skip-teardown`` was set.
        skip_data_collector (bool): Whether ``--skip-data-collector`` was set.

    Returns:
        bool: True when this call created the file, False when a snapshot already existed.
    """
    probes = _Probes(client=run_client) if run_client is not None else None
    sections = _session_start_identity_sections(
        probes=probes,
        pytest_args=pytest_args,
        skip_teardown=skip_teardown,
        skip_data_collector=skip_data_collector,
    )
    return _write_once_atomic(
        path=path,
        text=_redact_cluster_password(
            f"{_SECTION_SEPARATOR.join(_with_deadline_record(sections=sections, probes=probes))}\n"
        ),
    )


def write_session_baseline(
    *,
    pytest_args: Sequence[str],
    skip_teardown: bool,
    skip_data_collector: bool,
    client: DynamicClient | None = None,
) -> None:
    """Write the session-start cluster state and run identity once per session, never raising.

    Args:
        pytest_args (Sequence[str]): Arguments pytest was invoked with, including ``--tc`` overrides.
        skip_teardown (bool): Whether ``--skip-teardown`` was set.
        skip_data_collector (bool): Whether ``--skip-data-collector`` was set.
        client (DynamicClient | None): Run client; when None, one is built defensively from the
            resolved credentials so a container run without a client still gets its baseline.
    """
    if skip_data_collector:
        return

    collector_path = shared_data_collector_path()
    if collector_path is None:
        return

    directory = collector_path / _SESSION_START_DIRECTORY
    cluster_path = directory / _SESSION_START_CLUSTER_NAME
    identity_path = directory / _SESSION_START_RUN_IDENTITY_NAME
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if cluster_path.exists() and identity_path.exists():
            LOGGER.info(f"Session-start baseline already exists in {directory}, keeping the first snapshot")
            return
        run_client = client or _session_client()
        cluster_created = False
        if run_client is None:
            LOGGER.warning("Session-start baseline has no cluster client, writing the run identity only")
        else:
            cluster_created = _write_session_cluster_snapshot(run_client=run_client, path=cluster_path)
        identity_created = _write_session_identity(
            run_client=run_client,
            path=identity_path,
            pytest_args=pytest_args,
            skip_teardown=skip_teardown,
            skip_data_collector=skip_data_collector,
        )
    except (OSError, ValueError, TypeError, ClientWithBasicAuthError) as exc:
        # Best-effort evidence: a failing probe or a rejected credential skips the baseline instead of
        # aborting session start.
        LOGGER.warning(f"Session-start baseline could not be written to {directory}: {exc}")
        return

    if cluster_created and identity_created:
        LOGGER.warning(f"Session-start baseline written to {directory}")
        return
    if identity_created and not cluster_created:
        LOGGER.warning(f"Session-start run identity written to {directory}; cluster state skipped (no client)")
        return
    LOGGER.info(f"Session-start baseline already exists in {directory}, keeping the first snapshot")
