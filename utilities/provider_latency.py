"""Cluster-to-provider latency probing and pool-based source provider selection.

The probe measures TCP-connect time to the provider API port from one unprivileged pod
running on the destination OpenShift cluster - the same network path Forklift's
controller and populator pods use to reach the provider during migration. ICMP is not
used: provider endpoints commonly block it and restricted-SCC pods cannot send raw
ICMP (no ``NET_RAW``).
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from ocp_resources.namespace import Namespace
from ocp_resources.pod import Pod

from utilities.utils import load_source_providers

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

LOGGER = logging.getLogger(__name__)

# Public ubi9 python image: the probe script needs only a python3 interpreter.
PROBE_IMAGE = "registry.access.redhat.com/ubi9/python-314"
_DEFAULT_PROBE_PORT = 443  # Default provider API port when the endpoint does not specify one
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


def measure(target):
    host, _, port = target.rpartition(":")
    samples = []
    for _ in range(CONNECT_ATTEMPTS):
        start = time.perf_counter()
        try:
            socket.create_connection((host, int(port)), timeout=CONNECT_TIMEOUT).close()
        except OSError:
            return None
        samples.append((time.perf_counter() - start) * 1000.0)
    samples.sort()
    return samples[len(samples) // 2]


targets = sys.argv[1:]
with ThreadPoolExecutor(max_workers=min(16, len(targets))) as pool:
    results = list(pool.map(measure, targets))
print(json.dumps(dict(zip(targets, results))))
"""


def provider_feature_supported(provider_data: dict[str, Any], feature: str) -> bool:
    """Report whether a provider entry supports a requested feature.

    Args:
        provider_data (dict[str, Any]): One provider entry from `.providers.json`.
        feature (str): Requested feature key (e.g. `copyoffload`).

    Returns:
        bool: True when the provider entry carries the requested feature.

    Raises:
        ValueError: When the feature name is not supported.
    """
    if feature == "copyoffload":
        return "copyoffload" in provider_data
    raise ValueError(f"Unknown source_provider_feature {feature!r} (supported: 'copyoffload')")


def derive_provider_pool(
    providers: dict[str, dict[str, Any]],
    provider_type: str,
    provider_version: str,
    required_feature: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Derive the candidate provider pool for a type + version pair.

    Args:
        providers (dict[str, dict[str, Any]]): All source providers, keyed by name.
        provider_type (str): Required provider type (e.g. "vsphere").
        provider_version (str): Required provider version (e.g. "8.0.1").
        required_feature (str | None): Keep only providers supporting this feature.

    Returns:
        dict[str, dict[str, Any]]: Candidate providers, keyed by name.

    Raises:
        ValueError: When no provider matches the requested pool.
    """
    pool = {
        name: data
        for name, data in providers.items()
        if isinstance(data, dict)
        and data["type"] == provider_type
        and data["version"] == provider_version
        and (required_feature is None or provider_feature_supported(provider_data=data, feature=required_feature))
    }
    if not pool:
        raise ValueError(
            f"No provider matches type={provider_type} version={provider_version} "
            f"(required feature: {required_feature!r})"
        )
    return pool


def _port_from_api_url(api_url: str | None) -> int:
    """Extract an explicit TCP port from a provider API URL.

    Args:
        api_url (str | None): Provider API URL (e.g. "https://host:5000/path", "host/sdk").

    Returns:
        int: The explicit port, or the default provider API port when none is present.
    """
    if api_url and (match := re.search(r":(\d+)(?:/|$)", api_url)):
        return int(match.group(1))
    return _DEFAULT_PROBE_PORT


def provider_probe_endpoint(provider_data: dict[str, Any]) -> tuple[str, int] | None:
    """Resolve a provider's probe target from its configured endpoint.

    The host comes from the schema-required `fqdn`; the port comes from an explicit port
    in `api_url` (Hyper-V 5986, OpenStack 5000) and defaults to the provider API port.

    Args:
        provider_data (dict[str, Any]): One provider entry from `.providers.json`.

    Returns:
        tuple[str, int] | None: (host, port), or None for entries without `fqdn`
        (metadata entries and destination OpenShift providers).
    """
    host = provider_data.get("fqdn")
    if not host:
        return None
    return host, _port_from_api_url(api_url=provider_data.get("api_url"))


def probe_provider_latencies(client: DynamicClient, providers: dict[str, dict[str, Any]]) -> dict[str, float | None]:
    """Measure cluster-to-provider TCP-connect latency for every provider.

    One unprivileged probe pod runs on the cluster and connects to every provider's
    API endpoint in parallel. Providers sharing an endpoint are measured once.
    Entries without a resolvable endpoint (metadata, destination OpenShift providers)
    are reported as unreachable rather than probed.

    Args:
        client (DynamicClient): OpenShift client used to create the probe pod.
        providers (dict[str, dict[str, Any]]): Providers to probe, keyed by name.

    Returns:
        dict[str, float | None]: Median round-trip in milliseconds per provider name,
        or None when the provider endpoint is unreachable or not probeable.
    """
    endpoint_to_providers: dict[str, list[str]] = {}
    rtts: dict[str, float | None] = {}
    for name, data in providers.items():
        if not isinstance(data, dict):
            # File metadata such as `$schema` is not a provider.
            continue
        endpoint = provider_probe_endpoint(provider_data=data)
        if endpoint is None:
            rtts[name] = None
            continue
        endpoint_to_providers.setdefault(f"{endpoint[0]}:{endpoint[1]}", []).append(name)

    with _probe_pod(client=client, targets=list(endpoint_to_providers)) as pod:
        output = pod.execute(
            command=["python3", "-c", _PROBE_SCRIPT, *endpoint_to_providers],
            timeout=PROBE_SCRIPT_TIMEOUT_SECONDS,
        )
    endpoint_rtts: dict[str, float | None] = json.loads(_last_json_line(output=output))
    for target, names in endpoint_to_providers.items():
        for name in names:
            rtts[name] = endpoint_rtts.get(target)
    return rtts


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


def select_provider_from_pool(
    client: DynamicClient,
    providers_json_path: str | None,
    provider_type: str,
    provider_version: str,
    required_feature: str | None = None,
) -> dict[str, Any]:
    """Derive the candidate pool, probe it, and select the fastest member.

    Args:
        client (DynamicClient): OpenShift client used to run the probe pod.
        providers_json_path (str | None): Path to `.providers.json` (None resolves
            the standard location / `PROVIDERS_JSON_PATH` environment variable).
        provider_type (str): Required provider type (e.g. "vsphere").
        provider_version (str): Required provider version (e.g. "8.0.1").
        required_feature (str | None): Keep only providers supporting this feature.

    Returns:
        dict[str, Any]: `{"provider": <name>, "rtt_ms": <median round-trip>}`.

    Raises:
        ValueError: When the pool is empty or has no reachable member.
    """
    providers = load_source_providers(providers_json_path=providers_json_path)
    pool = derive_provider_pool(
        providers=providers,
        provider_type=provider_type,
        provider_version=provider_version,
        required_feature=required_feature,
    )
    LOGGER.info(f"Probing {len(pool)} candidate providers: {sorted(pool)}")
    rtts = probe_provider_latencies(client=client, providers=pool)
    selected, rtt = select_fastest_provider(providers=pool, rtts=rtts)
    return {"provider": selected, "rtt_ms": rtt}


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
def _probe_pod(client: DynamicClient, targets: list[str]) -> Iterator[Pod]:
    """Run a temporary probe pod for the duration of the context.

    The pod and its namespace are created only to carry the probe commands and are
    deleted on context exit; they are not test resources and never reach session
    teardown.

    Args:
        client (DynamicClient): OpenShift client used to create the resources.
        targets (list[str]): `host:port` targets the pod will probe.

    Yields:
        Pod: The running probe pod, ready for exec.
    """
    if not targets:
        raise ValueError("probe requires at least one provider target")
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
    "probe_provider_latencies",
    "provider_feature_supported",
    "provider_probe_endpoint",
    "select_fastest_provider",
    "select_provider_from_pool",
]
