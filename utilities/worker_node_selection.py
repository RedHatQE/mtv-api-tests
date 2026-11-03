from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ocp_resources.node import Node
from pytest_testconfig import config as py_config
from simple_logger.logger import get_logger

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient
    from ocp_utilities.monitoring import Prometheus

_WORKER_ROLE_LABEL = "node-role.kubernetes.io/worker"
_HOSTNAME_LABEL = "kubernetes.io/hostname"
_KIB = 1024  # Kubernetes memory quantities are normalized to bytes; 1 Ki = 1024 bytes
# Full Kubernetes memory quantity suffix set, case-sensitive per the Kubernetes spec
# (decimal SI: lowercase k, uppercase M/G/T/P/E; binary: Ki/Mi/Gi/Ti/Pi/Ei).
_MEMORY_SUFFIX_FACTORS = {
    "Ki": _KIB,
    "Mi": _KIB**2,
    "Gi": _KIB**3,
    "Ti": _KIB**4,
    "Pi": _KIB**5,
    "Ei": _KIB**6,
    "k": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
    "P": 1000**5,
    "E": 1000**6,
}

LOGGER = get_logger(__name__)


def get_worker_nodes(ocp_client: DynamicClient) -> list[str]:
    """Get list of worker node names from the cluster.

    Args:
        ocp_client (DynamicClient): OpenShift DynamicClient instance.

    Returns:
        list[str]: List of worker node names.

    Raises:
        Exception: If Node.get() encounters client or API errors from OpenShift DynamicClient.
    """
    return [node.name for node in get_worker_node_resources(ocp_client=ocp_client)]


def get_node_labels(node: Node) -> dict[str, str]:
    """Return labels from a Node's Kubernetes metadata.

    Args:
        node (Node): Node resource to read.

    Returns:
        dict[str, str]: Node labels, or an empty dictionary when none are set.
    """
    return dict(node.instance.get("metadata", {}).get("labels") or {})


def get_worker_node_resources(ocp_client: DynamicClient, nodes: list[Node] | None = None) -> list[Node]:
    """Get worker Node resources from the cluster.

    Args:
        ocp_client (DynamicClient): OpenShift DynamicClient instance.
        nodes (list[Node] | None): Previously listed Nodes, avoiding a second API request when provided.

    Returns:
        list[Node]: Worker Node resources.
    """
    all_nodes = nodes if nodes is not None else Node.get(client=ocp_client)
    return [node for node in all_nodes if _WORKER_ROLE_LABEL in get_node_labels(node=node)]


def get_ready_schedulable_worker_nodes(
    ocp_client: DynamicClient,
    nodes: list[Node] | None = None,
) -> list[Node]:
    """Get Ready, schedulable workers without blocking taints.

    Args:
        ocp_client (DynamicClient): OpenShift client used to list Nodes when not supplied.
        nodes (list[Node] | None): Previously listed Nodes, avoiding a second API request when provided.

    Returns:
        list[Node]: Ready workers that are not cordoned and have no NoSchedule or NoExecute taints.
    """
    return [
        node
        for node in get_worker_node_resources(ocp_client=ocp_client, nodes=nodes)
        if any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in node.instance.get("status", {}).get("conditions") or []
        )
        and not node.instance.get("spec", {}).get("unschedulable", False)
        and not any(
            taint.get("effect") in {"NoSchedule", "NoExecute"}
            for taint in node.instance.get("spec", {}).get("taints") or []
        )
    ]


def get_session_unique_label_key(label_key: str, session_uuid: str) -> str:
    """Append a session identifier to a Kubernetes label key without exceeding 63 characters.

    Args:
        label_key (str): Label key to make unique within the test session.
        session_uuid (str): Unique pytest session identifier.

    Returns:
        str: Session-specific label key.
    """
    suffix = f"-{session_uuid}"
    if "/" in label_key:
        prefix, name = label_key.split("/", 1)
        return f"{prefix}/{name[: 63 - len(suffix)]}{suffix}"
    return f"{label_key[: 63 - len(suffix)]}{suffix}"


def _query_prometheus_safe(prometheus: Prometheus, query: str, metric_name: str) -> list[dict[str, Any]]:
    """Query Prometheus and return result list, or [] on failure.

    Args:
        prometheus (Prometheus): Prometheus client instance.
        query (str): Prometheus query string.
        metric_name (str): Metric name for logging purposes.

    Returns:
        list[dict[str, Any]]: Query results or empty list on failure.

    Raises:
        None: Exceptions are caught internally and empty list is returned.
    """
    try:
        response = prometheus.query(query=query)
        return response.get("data", {}).get("result", []) if response else []
    except Exception as e:
        LOGGER.warning("Prometheus %s query failed: %s", metric_name, e)
        return []


def parse_prometheus_value(raw_value: object) -> int:
    """Parse Prometheus metric value to integer.

    Args:
        raw_value (object): Raw value from Prometheus response, typically [timestamp, value].

    Returns:
        int: Parsed integer value, or 0 if parsing fails.

    Raises:
        None: Parsing errors are caught internally and 0 is returned.
    """
    if (
        isinstance(raw_value, (list, tuple))
        and len(raw_value) >= 2
        and isinstance(raw_value[1], (str, bytes, int, float))
    ):
        try:
            return int(float(raw_value[1]))
        except (ValueError, TypeError):
            return 0
    return 0


def parse_prometheus_memory_metrics(worker_nodes: list[str], prometheus: Prometheus) -> dict[str, dict[str, int]]:
    """Query Prometheus for memory metrics and return structured data.

    Args:
        worker_nodes (list[str]): List of worker node names to query metrics for.
        prometheus (Prometheus): Prometheus client instance for querying metrics.

    Returns:
        dict[str, dict[str, int]]: Dictionary mapping node names to memory metrics (allocatable, requested, available).
            Returns empty dict if query fails or no metrics available.

    Raises:
        None: Does not raise exceptions; query errors are handled internally by _query_prometheus_safe.
    """
    worker_nodes_set = set(worker_nodes)
    allocatable_query = 'kube_node_status_allocatable{resource="memory"}'
    requested_query = (
        "sum by (node) ("
        'kube_pod_container_resource_requests{resource="memory"} '
        "* on(namespace, pod) group_left() "
        '(kube_pod_status_phase{phase="Running"} == 1)'
        ")"
    )

    allocatable_result = _query_prometheus_safe(prometheus, allocatable_query, "allocatable")
    if not allocatable_result:
        return {}

    requested_result = _query_prometheus_safe(prometheus, requested_query, "requested")

    metrics: dict[str, dict[str, int]] = {}
    for item in allocatable_result:
        node = item.get("metric", {}).get("node")
        if node in worker_nodes_set:
            raw_value = item.get("value")
            value = parse_prometheus_value(raw_value)
            metrics.setdefault(node, {})["allocatable"] = value

    if requested_result:
        for item in requested_result:
            node = item.get("metric", {}).get("node")
            if node in worker_nodes_set and node in metrics:
                raw_value = item.get("value")
                value = parse_prometheus_value(raw_value)
                metrics[node]["requested"] = value

    for node in metrics:
        metrics[node].setdefault("requested", 0)
        metrics[node]["available"] = max(0, metrics[node]["allocatable"] - metrics[node]["requested"])

    return metrics


def _get_node_with_most_memory(metrics: dict[str, dict[str, int]]) -> str:
    """Get the node with the most available memory from metrics.

    Args:
        metrics (dict[str, dict[str, int]]): Memory metrics by node

    Returns:
        str: Node name with most available memory

    Raises:
        ValueError: If metrics dictionary is empty
    """
    if not metrics:
        raise ValueError("Cannot select node: metrics dictionary is empty")

    max_available = max(node_metrics["available"] for node_metrics in metrics.values())
    nodes_with_max = [node for node, node_metrics in metrics.items() if node_metrics["available"] == max_available]
    return nodes_with_max[0]


def _create_prometheus_client(ocp_admin_client: DynamicClient) -> Prometheus | None:
    """Create and initialize Prometheus client for metrics queries.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin DynamicClient with cluster access.

    Returns:
        Prometheus | None: Initialized Prometheus client, or None if initialization fails.
    """
    from ocp_utilities.monitoring import Prometheus  # noqa: PLC0415

    # Auth header format: "Bearer <token>" - extract just the token part
    auth_header = ocp_admin_client.configuration.api_key.get("authorization", "")
    token_parts = auth_header.split()
    token = token_parts[-1] if token_parts else ""
    if not token:
        LOGGER.warning("No auth token available, cannot create Prometheus client")
        return None

    verify_ssl = py_config["insecure_verify_skip"].lower() != "true"

    try:
        return Prometheus(
            bearer_token=token,
            namespace="openshift-monitoring",
            resource_name="thanos-querier",
            client=ocp_admin_client,
            verify_ssl=verify_ssl,
        )
    except Exception as e:
        LOGGER.warning("Failed to initialize Prometheus client: %s", e)
        return None


def select_node_by_available_memory(
    ocp_admin_client: DynamicClient,
    worker_nodes: list[str],
) -> str:
    """Select worker node with highest available memory using Prometheus metrics.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin DynamicClient with cluster access.
        worker_nodes (list[str]): List of worker node names to select from.

    Returns:
        str: Name of the selected worker node.

    Note:
        Falls back to first node if auth token, Prometheus client, or memory metrics are unavailable.
    """
    prometheus = _create_prometheus_client(ocp_admin_client)
    if not prometheus:
        LOGGER.warning("Prometheus client unavailable, selecting first worker node")
        return worker_nodes[0]

    metrics = parse_prometheus_memory_metrics(worker_nodes, prometheus)
    if not metrics:
        LOGGER.info("No valid memory metrics available (Prometheus may not have metrics), selecting first worker node")
        return worker_nodes[0]

    selected_node = _get_node_with_most_memory(metrics)

    LOGGER.info("Selected node %s with highest available memory for scheduling", selected_node)

    return selected_node


def _parse_kubernetes_memory_quantity(quantity: str) -> int:
    """Parse a Kubernetes memory quantity string to bytes.

    Args:
        quantity (str): Kubernetes quantity string (e.g. "15306Mi", "16Gi", "500k", "1Ei",
            "123456789"). A bare number is bytes. "m" (milli) is sub-byte for memory and
            rounds to 0 bytes.

    Returns:
        int: Quantity in bytes.

    Raises:
        ValueError: If the quantity string does not match a known Kubernetes format.
    """
    for suffix, factor in _MEMORY_SUFFIX_FACTORS.items():
        if quantity.endswith(suffix):
            return int(float(quantity[: -len(suffix)]) * factor)
    if quantity.endswith("m"):  # milli: sub-byte for memory, rounds to 0 bytes
        return 0
    try:
        return int(float(quantity))
    except ValueError:
        raise ValueError(f"Unparsable Kubernetes memory quantity '{quantity}'") from None


def _get_container_memory_request(container: dict[str, Any], pod_name: str) -> int:
    """Get a single container's memory request in bytes, with shape validation.

    Args:
        container (dict[str, Any]): Raw container entry from a Pod spec.
        pod_name (str): Pod name for error context.

    Returns:
        int: Requested memory in bytes; 0 when no request is set (absence is valid).

    Raises:
        ValueError: If the container entry or its memory request is malformed/unparsable.
    """
    if not isinstance(container, dict):
        raise ValueError(f"Pod '{pod_name}' has a malformed container entry: {container!r}")
    resources = container.get("resources")
    requests = resources.get("requests") if isinstance(resources, dict) else None
    memory = requests.get("memory") if isinstance(requests, dict) else None
    if memory is None:
        return 0
    if not isinstance(memory, str):
        raise ValueError(f"Pod '{pod_name}' has a non-string memory request: {memory!r}")
    try:
        return _parse_kubernetes_memory_quantity(memory)
    except ValueError:
        raise ValueError(f"Pod '{pod_name}' has an unparsable memory request '{memory}'") from None


def _get_pod_memory_request(pod: dict[str, Any]) -> int:
    """Get the effective memory request of a pod.

    Kubernetes schedules a pod based on the larger of the sum of regular container
    requests and the largest init-container request
    (max(sum(containers), max(initContainers)));

    Args:
        pod (dict[str, Any]): Raw Pod resource as returned by the dynamic API.

    Returns:
        int: Effective requested memory in bytes for the pod.

    Raises:
        ValueError: If a container entry, memory quantity, or pod overhead is malformed.
    """
    pod_name = (pod.get("metadata") or {}).get("name") or "<unnamed>"
    spec = pod.get("spec") or {}
    containers = spec.get("containers") or []
    init_containers = spec.get("initContainers") or []
    regular_sum = sum(_get_container_memory_request(container, pod_name) for container in containers)
    init_max = max((_get_container_memory_request(container, pod_name) for container in init_containers), default=0)
    # Kubernetes pod scheduling rule: max(sum(regular container requests), max(init container requests))
    # plus the pod's spec.overhead (runtime overhead charged on top of container requests)
    overhead = spec.get("overhead")
    overhead_memory = overhead.get("memory") if isinstance(overhead, dict) else None
    if overhead_memory is None:
        overhead_bytes = 0
    else:
        if not isinstance(overhead_memory, str):
            raise ValueError(f"Pod '{pod_name}' has a non-string overhead memory: {overhead_memory!r}")
        try:
            overhead_bytes = _parse_kubernetes_memory_quantity(overhead_memory)
        except ValueError:
            raise ValueError(f"Pod '{pod_name}' has an unparsable overhead memory '{overhead_memory}'") from None
    return max(regular_sum, init_max) + overhead_bytes


def select_node_by_requestable_memory(
    ocp_client: DynamicClient,
    node_names: list[str],
) -> dict[str, str]:
    """Select the worker node with the most requestable memory.

    Requestable memory is the node's allocatable memory minus the sum of memory
    requests of pods scheduled on it (non-terminal phases only).

    Args:
        ocp_client (DynamicClient): OpenShift DynamicClient instance.
        node_names (list[str]): Worker node names to select from.

    Returns:
        dict with keys: node_name (the node's metadata.name, as the scheduler reports
            it) and hostname_label (the node's actual kubernetes.io/hostname label value,
            which may differ from the node name and is what node selectors match).

    Raises:
        ValueError: If node_names is empty, no allocatable memory is found for the given
            nodes, or the selected node lacks the kubernetes.io/hostname label.
    """
    if not node_names:
        raise ValueError("Cannot select node: node_names is empty")

    node_names_set = set(node_names)
    requestable_memory: dict[str, int] = {}
    nodes_by_name: dict[str, Any] = {}

    nodes = ocp_client.resources.get(api_version="v1", kind="Node").get().items
    for node in nodes:
        node_name = node.metadata.name
        if node_name in node_names_set:
            nodes_by_name[node_name] = node
            allocatable = node.status.allocatable.get("memory")
            if allocatable:
                requestable_memory[node_name] = _parse_kubernetes_memory_quantity(allocatable)

    if not requestable_memory:
        raise ValueError("No allocatable memory found for the given worker nodes")

    pods = ocp_client.resources.get(api_version="v1", kind="Pod").get().items
    for pod in pods:
        pod_dict = pod.to_dict()
        pod_node_name = pod_dict.get("spec", {}).get("nodeName")
        pod_phase = pod_dict.get("status", {}).get("phase")
        if pod_node_name in requestable_memory and pod_phase not in {"Succeeded", "Failed"}:
            requestable_memory[pod_node_name] -= _get_pod_memory_request(pod=pod_dict)

    selected_node_name = max(requestable_memory, key=lambda node_name: requestable_memory[node_name])
    selected_node = nodes_by_name[selected_node_name]
    hostname_label = (selected_node.metadata.labels or {}).get(_HOSTNAME_LABEL)
    if not isinstance(hostname_label, str) or not hostname_label:
        raise ValueError(
            f"Selected node '{selected_node_name}' has an invalid {_HOSTNAME_LABEL} label value: "
            f"{hostname_label!r} (expected a non-empty string)"
        )

    return {"node_name": selected_node_name, "hostname_label": hostname_label}
