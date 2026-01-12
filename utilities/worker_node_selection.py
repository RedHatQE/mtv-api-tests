from __future__ import annotations

import logging
import random
from typing import TYPE_CHECKING, Any

from ocp_resources.node import Node
from ocp_resources.resource import ResourceEditor
from ocp_utilities.monitoring import Prometheus
from pytest_testconfig import config as py_config

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

LOGGER = logging.getLogger(__name__)


def get_worker_nodes(ocp_client: DynamicClient) -> list[str]:
    """Get list of worker node names from the cluster.

    Args:
        ocp_client: OpenShift DynamicClient instance.

    Returns:
        List of worker node names.
    """
    return [
        node.name
        for node in Node.get(client=ocp_client)
        if node.labels and "node-role.kubernetes.io/worker" in node.labels.keys()
    ]


def _query_prometheus_safe(prometheus: Prometheus, query: str, metric_name: str) -> list[dict[str, Any]]:
    """Query Prometheus and return result list, or [] on failure."""
    try:
        response = prometheus.query(query=query)
        return response.get("data", {}).get("result", []) if response else []
    except Exception as e:
        LOGGER.warning(f"Prometheus {metric_name} query failed: {e}")
        return []


def parse_prometheus_value(raw_value: Any) -> int:
    """Parse Prometheus metric value to integer.

    Args:
        raw_value: Raw value from Prometheus response, typically [timestamp, value].

    Returns:
        Parsed integer value, or 0 if parsing fails.
    """
    if isinstance(raw_value, (list, tuple)) and len(raw_value) >= 2 and raw_value[1]:
        try:
            return int(float(raw_value[1]))
        except (ValueError, TypeError):
            return 0
    return 0


def parse_prometheus_memory_metrics(
    worker_nodes: list[str], prometheus: Prometheus
) -> dict[str, dict[str, int]] | None:
    """Query Prometheus for memory metrics and return structured data.

    Args:
        worker_nodes: List of worker node names to query metrics for.
        prometheus: Prometheus client instance for querying metrics.

    Returns:
        Dictionary mapping node names to memory metrics (allocatable, requested, available),
        or None if query fails or no metrics available. Catches all exceptions and logs warnings.
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
        return None

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

    return metrics if metrics else None


def select_node_by_available_memory(
    ocp_admin_client: DynamicClient,
    worker_nodes: list[str],
) -> str:
    """Select worker node with highest available memory using Prometheus metrics.

    Args:
        ocp_admin_client: OpenShift admin DynamicClient with cluster access.
        worker_nodes: List of worker node names to select from.

    Returns:
        Name of the selected worker node.

    Raises:
        ValueError: If worker_nodes list is empty.

    Note:
        Falls back to random selection if auth token, Prometheus client, or memory metrics are unavailable.
    """
    if not worker_nodes:
        raise ValueError("No worker nodes available for selection")

    # Auth header format: "Bearer <token>" - extract just the token part
    auth_header = ocp_admin_client.configuration.api_key.get("authorization", "")
    token_parts = auth_header.split()
    token = token_parts[-1] if token_parts else ""
    if not token:
        LOGGER.warning("No auth token available, selecting random worker node")
        return random.choice(worker_nodes)

    verify_ssl = py_config["insecure_verify_skip"].lower() != "true"

    try:
        prometheus = Prometheus(
            bearer_token=token,
            namespace="openshift-monitoring",
            resource_name="thanos-querier",
            client=ocp_admin_client,
            verify_ssl=verify_ssl,
        )
    except Exception as e:
        LOGGER.warning(f"Failed to initialize Prometheus client: {e}, selecting random worker node")
        return random.choice(worker_nodes)

    metrics = parse_prometheus_memory_metrics(worker_nodes, prometheus)
    if not metrics:
        LOGGER.info("No valid memory metrics available (Prometheus may not have metrics), selecting random worker node")
        return random.choice(worker_nodes)

    max_available = max(node_metrics["available"] for node_metrics in metrics.values())
    nodes_with_max = [node for node, node_metrics in metrics.items() if node_metrics["available"] == max_available]
    selected_node = random.choice(nodes_with_max)

    LOGGER.info(f"Selected node {selected_node} with highest available memory for scheduling")

    return selected_node


def label_node(ocp_client: DynamicClient, node_name: str, label_key: str, label_value: str) -> None:
    """Apply label to a node.

    Args:
        ocp_client: OpenShift DynamicClient instance.
        node_name: Name of the node to label.
        label_key: Label key to apply.
        label_value: Label value to set.

    Raises:
        RuntimeError: If labeling operation fails.
    """
    try:
        node = Node(client=ocp_client, name=node_name)
        ResourceEditor(patches={node: {"metadata": {"labels": {label_key: label_value}}}}).update()
    except Exception as e:
        raise RuntimeError(f"Failed to label node {node_name} with {label_key}={label_value}: {e}") from e


def cleanup_node_label(ocp_client: DynamicClient, node_name: str, label_key: str) -> None:
    """Remove label from node using strategic merge patch.

    Args:
        ocp_client: OpenShift DynamicClient instance.
        node_name: Name of the node to remove label from.
        label_key: Label key to remove.

    Note:
        Logs warning instead of raising exception if cleanup fails.
    """
    try:
        node = Node(client=ocp_client, name=node_name)
        ResourceEditor(patches={node: {"metadata": {"labels": {label_key: None}}}}).update()
        LOGGER.info(f"Removed label {label_key} from node {node_name}")
    except Exception as e:
        LOGGER.warning(f"Failed to cleanup label {label_key} from node {node_name}: {e}")
