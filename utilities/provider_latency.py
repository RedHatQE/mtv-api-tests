"""Cluster-to-provider latency probing and pool-based source provider selection.

The probe measures TCP-connect time to the provider API port from one unprivileged pod
running on the destination OpenShift cluster - the same network path Forklift's
controller and populator pods use to reach the provider during migration. ICMP is not
used: provider endpoints commonly block it and restricted-SCC pods cannot send raw
ICMP (no ``NET_RAW``).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ocp_resources.namespace import Namespace
from ocp_resources.pod import Pod

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

# Public ubi9 python image: the probe script needs only a python3 interpreter.
PROBE_IMAGE = "registry.access.redhat.com/ubi9/python-314"
PROBE_PORT = 443  # Provider API port, the port Forklift itself connects to
PROBE_CONNECT_ATTEMPTS = 3  # Median of three TCP connects per provider endpoint
PROBE_CONNECT_TIMEOUT_SECONDS = 5
PROBE_POD_START_TIMEOUT_SECONDS = 300  # First run pulls the image from the registry
PROBE_SCRIPT_TIMEOUT_SECONDS = 120
PROBE_POD_IDLE_SECONDS = 600  # The pod sleeps while probe commands are exec'd into it

# The script below runs inside the probe pod's python3, not in the test runner. It
# measures every endpoint in parallel and prints one JSON object to stdout.
_PROBE_SCRIPT = f"""
import json
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor

CONNECT_ATTEMPTS = {PROBE_CONNECT_ATTEMPTS}
CONNECT_TIMEOUT = {PROBE_CONNECT_TIMEOUT_SECONDS}
PORT = {PROBE_PORT}


def measure(host):
    samples = []
    for _ in range(CONNECT_ATTEMPTS):
        start = time.perf_counter()
        try:
            socket.create_connection((host, PORT), timeout=CONNECT_TIMEOUT).close()
        except OSError:
            return None
        samples.append((time.perf_counter() - start) * 1000.0)
    samples.sort()
    return samples[len(samples) // 2]


hosts = sys.argv[1:]
with ThreadPoolExecutor(max_workers=min(16, len(hosts))) as pool:
    results = list(pool.map(measure, hosts))
print(json.dumps(dict(zip(hosts, results))))
"""


def provider_supports_copyoffload(provider_data: dict[str, Any]) -> bool:
    """Report whether a provider entry carries copy-offload (XCOPY) configuration.

    Args:
        provider_data (dict[str, Any]): One provider entry from `.providers.json`.

    Returns:
        bool: True when the entry contains a `copyoffloadConfig` section.
    """
    return "copyoffloadConfig" in provider_data


def derive_provider_pool(
    providers: dict[str, dict[str, Any]],
    provider_type: str,
    provider_version: str,
    require_copyoffload: bool = False,
) -> dict[str, dict[str, Any]]:
    """Derive the candidate provider pool for a type + version pair.

    Args:
        providers (dict[str, dict[str, Any]]): All source providers, keyed by name.
        provider_type (str): Required provider type (e.g. "vsphere").
        provider_version (str): Required provider version (e.g. "8.0.1").
        require_copyoffload (bool): Keep only copy-offload capable providers.

    Returns:
        dict[str, dict[str, Any]]: Candidate providers, keyed by name.

    Raises:
        ValueError: When no provider matches the requested pool.
    """
    pool = {
        name: data
        for name, data in providers.items()
        if data["type"] == provider_type
        and data["version"] == provider_version
        and (not require_copyoffload or provider_supports_copyoffload(provider_data=data))
    }
    if not pool:
        raise ValueError(
            f"No provider matches type={provider_type} version={provider_version} "
            f"(copyoffload required: {require_copyoffload})"
        )
    return pool


def probe_provider_latencies(client: DynamicClient, providers: dict[str, dict[str, Any]]) -> dict[str, float | None]:
    """Measure cluster-to-provider TCP-connect latency for every provider.

    One unprivileged probe pod runs on the cluster and connects to every provider's
    API endpoint in parallel. Providers sharing an endpoint are measured once.

    Args:
        client (DynamicClient): OpenShift client used to create the probe pod.
        providers (dict[str, dict[str, Any]]): Providers to probe, keyed by name.

    Returns:
        dict[str, float | None]: Median round-trip in milliseconds per provider name,
        or None when the provider endpoint is unreachable.
    """
    endpoint_to_providers: dict[str, list[str]] = {}
    for name, data in providers.items():
        endpoint_to_providers.setdefault(data["fqdn"], []).append(name)

    with _probe_pod(client=client, endpoints=list(endpoint_to_providers)) as pod:
        output = pod.execute(
            command=["python3", "-c", _PROBE_SCRIPT, *endpoint_to_providers],
            timeout=PROBE_SCRIPT_TIMEOUT_SECONDS,
        )
    endpoint_rtts: dict[str, float | None] = json.loads(_last_json_line(output=output))
    return {name: endpoint_rtts.get(endpoint) for endpoint, names in endpoint_to_providers.items() for name in names}


def select_fastest_provider(providers: dict[str, dict[str, Any]], rtts: dict[str, float | None]) -> tuple[str, float]:
    """Pick the lowest-RTT provider with a deterministic tiebreak.

    Args:
        providers (dict[str, dict[str, Any]]): Candidate pool, keyed by name.
        rtts (dict[str, float | None]): Measured RTTs, keyed by provider name.

    Returns:
        tuple[str, float]: Selected provider name and its round-trip in milliseconds.

    Raises:
        ValueError: When no candidate is reachable.
    """
    reachable = {name: rtt for name, rtt in rtts.items() if rtt is not None}
    if not reachable:
        raise ValueError(f"No reachable provider among {sorted(providers)}")
    winner = min(reachable, key=lambda name: (reachable[name], name))
    return winner, reachable[winner]


def format_latency_table(providers: dict[str, dict[str, Any]], rtts: dict[str, float | None]) -> str:
    """Render a ranked latency table grouped by provider type and version.

    Args:
        providers (dict[str, dict[str, Any]]): Probed providers, keyed by name.
        rtts (dict[str, float | None]): Measured RTTs, keyed by provider name.

    Returns:
        str: Plain-text table, groups sorted by type+version, rows sorted by RTT.
    """
    groups: dict[tuple[str, str], list[tuple[str, float | None]]] = {}
    for name, rtt in rtts.items():
        data = providers[name]
        groups.setdefault((data["type"], data["version"]), []).append((name, rtt))

    lines = [
        f"Cluster-to-provider latency (TCP connect to :{PROBE_PORT}, median of {PROBE_CONNECT_ATTEMPTS} connects):"
    ]
    for (provider_type, provider_version), rows in sorted(groups.items()):
        lines.append(f"\n{provider_type} {provider_version}:")
        lines.append("  Provider                             RTT (ms)")
        lines.append("  -----------------------------------  ----------")
        for name, rtt in sorted(rows, key=lambda row: (row[1] is None, row[1] or 0, row[0])):
            rtt_text = "unreachable" if rtt is None else f"{rtt:.1f}"
            lines.append(f"  {name:<35}  {rtt_text:>10}")
    return "\n".join(lines)


def _last_json_line(output: str) -> str:
    """Return the last JSON object line from pod exec output.

    Args:
        output (str): Full stdout of the probe command.

    Returns:
        str: The last line that parses as a JSON object.

    Raises:
        ValueError: When no line parses as a JSON object.
    """
    for line in reversed(output.strip().splitlines()):
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            return stripped
    raise ValueError(f"No JSON object found in probe output: {output!r}")


@contextmanager
def _probe_pod(client: DynamicClient, endpoints: list[str]) -> Iterator[Pod]:
    """Run a temporary probe pod for the duration of the context.

    The pod and its namespace are created only to carry the probe commands and are
    deleted on context exit; they are not test resources and never reach session
    teardown.

    Args:
        client (DynamicClient): OpenShift client used to create the resources.
        endpoints (list[str]): Provider endpoints the pod will probe.

    Yields:
        Pod: The running probe pod, ready for exec.
    """
    if not endpoints:
        raise ValueError("probe requires at least one provider endpoint")
    unique = uuid.uuid4().hex[:8]
    namespace = Namespace(
        client=client,
        name=f"latency-probe-{unique}",
    )
    pod = Pod(
        client=client,
        name=f"latency-probe-{unique}",
        namespace=namespace.name,
        command=["sleep", str(PROBE_POD_IDLE_SECONDS)],
        image=PROBE_IMAGE,
    )
    try:
        namespace.deploy()
        pod.deploy()
        pod.wait_for_status(status=Pod.Status.RUNNING, timeout=PROBE_POD_START_TIMEOUT_SECONDS)
        yield pod
    finally:
        pod.clean_up(wait=False)
        namespace.clean_up(wait=True)


__all__ = [
    "PROBE_IMAGE",
    "derive_provider_pool",
    "format_latency_table",
    "probe_provider_latencies",
    "provider_supports_copyoffload",
    "select_fastest_provider",
]
