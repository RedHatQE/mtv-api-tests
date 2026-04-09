from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ocp_resources.node import Node
from pytest_testconfig import config as py_config
from simple_logger.logger import get_logger

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient
    from ocp_utilities.monitoring import Prometheus

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
    return [
        node.name
        for node in Node.get(client=ocp_client)
        if node.labels and "node-role.kubernetes.io/worker" in node.labels.keys()
    ]


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
        except ValueError, TypeError:
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
