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
import os
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# `Any` is the value type of `.providers.json` entries: schema-validated JSON mappings
# whose fields vary per provider type (see providers_schema.json), so no narrower static
# type exists for them in this codebase.
from typing import TYPE_CHECKING, Any

from ocp_resources.namespace import Namespace
from ocp_resources.pod import Pod

from utilities.utils import load_source_providers

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

# Public ubi9 python image: the probe script needs only a python3 interpreter.
PROBE_IMAGE = "registry.access.redhat.com/ubi9/python-314"
_DEFAULT_PROBE_PORT = 443  # Default provider API port when the endpoint does not specify one
# Type-based probe ports when the endpoint does not specify one: Hyper-V WSMan listens
# on 5986 behind a bare-hostname api_url; OVA entries are NFS exports probed on 2049.
_PROBE_PORTS_BY_TYPE = {"hyperv": 5986, "ova": 2049}
PROBE_CONNECT_ATTEMPTS = 3  # Median of three TCP connects per provider endpoint
PROBE_CONNECT_TIMEOUT_SECONDS = 5
PROBE_POD_START_TIMEOUT_SECONDS = 300  # First run pulls the image from the registry
PROBE_SCRIPT_TIMEOUT_SECONDS = 120
PROBE_POD_IDLE_SECONDS = 600  # The pod sleeps while probe commands are exec'd into it

LOGGER = logging.getLogger(__name__)

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


def _validate_feature_name(feature: str | None) -> None:
    """Fail fast on feature names the pool derivation does not support.

    Args:
        feature (str | None): Requested feature key (e.g. `copyoffload`).

    Raises:
        ValueError: When the feature name is not supported.
    """
    if feature is not None and feature != "copyoffload":
        raise ValueError(f"Unknown source_provider_feature {feature!r} (supported: 'copyoffload')")


def provider_feature_supported(provider_data: dict[str, Any], feature: str | None) -> bool:
    """Report whether a provider entry passes the requested feature gate.

    Args:
        provider_data (dict[str, Any]): One provider entry from `.providers.json`.
        feature (str | None): Requested feature key (e.g. `copyoffload`). None means no
            feature is requested — copy-offload-capable providers are then excluded, so
            plain runs never land on a copy-offload provider.

    Returns:
        bool: True when the provider belongs in the pool.

    Raises:
        ValueError: When the feature name is not supported.
    """
    _validate_feature_name(feature=feature)
    return ("copyoffload" in provider_data) == (feature == "copyoffload")


def derive_provider_pool(
    providers: dict[str, dict[str, Any]],
    provider_type: str,
    provider_version: str | None = None,
    required_feature: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Derive the candidate provider pool for a provider type.

    Args:
        providers (dict[str, dict[str, Any]]): All source providers, keyed by name.
        provider_type (str): Required provider type (e.g. "vsphere").
        provider_version (str | None): Required provider version (e.g. "8.0.1");
            None pools every version of the type.
        required_feature (str | None): Keep only providers supporting this feature.
            Copy-offload-capable providers are excluded unless it is `copyoffload`.

    Returns:
        dict[str, dict[str, Any]]: Candidate providers, keyed by name.

    Raises:
        ValueError: When the feature name is not supported, or no provider matches
            the requested pool.
    """
    # Validate the feature name before filtering so a bad name is reported even
    # when the type/version pair already matches nothing.
    _validate_feature_name(feature=required_feature)
    pool = {
        name: provider_data
        for name, provider_data in providers.items()
        if isinstance(provider_data, dict)
        and provider_data["type"] == provider_type
        and (provider_version is None or provider_data["version"] == provider_version)
        and provider_feature_supported(provider_data=provider_data, feature=required_feature)
    }
    if not pool:
        raise ValueError(
            f"No provider matches type={provider_type} version={provider_version} "
            f"(required feature: {required_feature!r})"
        )
    return pool


def _port_from_api_url(api_url: str | None, default_port: int) -> int:
    """Extract an explicit TCP port from a provider API URL's authority.

    Only the URL authority (the `host:port` part before any path) is parsed, so digits in
    an NFS export path (e.g. `nfs-host:/exports/vm:5000`) never masquerade as a port.

    Args:
        api_url (str | None): Provider API URL (e.g. "https://host:5000/v3") or NFS
            export path ("nfs-host:/export/path").
        default_port (int): Port to use when the URL carries no explicit one.

    Returns:
        int: The explicit port, or `default_port` when none is present.
    """
    if not api_url:
        return default_port
    url = api_url.split("://", 1)[1] if "://" in api_url else api_url
    authority = url.split("/", 1)[0]
    if match := re.search(r":(\d+)$", authority):
        return int(match.group(1))
    return default_port


def _host_from_api_url(api_url: str | None) -> str | None:
    """Extract the host from a URL or an NFS export path.

    Args:
        api_url (str | None): Provider API URL ("https://host:5000/v3") or NFS export
            path ("nfs-host:/export/path"); None when the entry carries none.

    Returns:
        str | None: The host, or None when the URL carries none.
    """
    if not api_url:
        return None
    if "://" in api_url:
        api_url = api_url.split("://", 1)[1]
    authority = api_url.split("/", 1)[0]
    host = authority.rpartition("@")[2] if "@" in authority else authority
    if host.startswith("[") and host.endswith("]"):
        return host[1:-1]
    if ":" in host:
        host = host.rpartition(":")[0]
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
    return host or None


def provider_probe_endpoint(provider_data: dict[str, Any]) -> tuple[str, int]:
    """Resolve a provider's probe target from its configured endpoint.

    The host comes from `fqdn` when set, else from `api_url` (a URL authority or an NFS
    export path — OVA entries carry an empty `fqdn` and an NFS `api_url`), else from the
    OpenShift `host` field. The port comes from an explicit port in `api_url` and
    otherwise defaults to the provider type's API port (OVA NFS 2049, Hyper-V 5986,
    everything else 443).

    Args:
        provider_data (dict[str, Any]): One provider entry from `.providers.json`.

    Returns:
        tuple[str, int]: The (host, port) probe target.

    Raises:
        ValueError: When no endpoint host can be resolved from the entry.
    """
    host = (
        provider_data.get("fqdn")
        or _host_from_api_url(api_url=provider_data.get("api_url"))
        or provider_data.get("host")
    )
    if not host:
        raise ValueError(
            f"Provider entry of type {provider_data['type']} has no resolvable probe endpoint "
            "(fqdn, api_url, and host are all empty)"
        )
    return (
        host,
        _port_from_api_url(
            api_url=provider_data.get("api_url"),
            default_port=_PROBE_PORTS_BY_TYPE.get(provider_data["type"], _DEFAULT_PROBE_PORT),
        ),
    )


def probe_provider_latencies(client: DynamicClient, providers: dict[str, dict[str, Any]]) -> dict[str, float | None]:
    """Measure cluster-to-provider TCP-connect latency for every provider.

    One unprivileged probe pod runs on the cluster and connects to every provider's
    API endpoint in parallel. Providers sharing an endpoint are measured once. File
    metadata entries such as `$schema` are skipped; a provider entry lacking its
    schema-required `fqdn` fails loudly via `provider_probe_endpoint`.

    Args:
        client (DynamicClient): OpenShift client used to create the probe pod.
        providers (dict[str, dict[str, Any]]): Providers to probe, keyed by name.

    Returns:
        dict[str, float | None]: Median round-trip in milliseconds per provider name,
        or None when the provider endpoint is unreachable.
    """
    endpoint_to_providers: dict[str, list[str]] = {}
    for name, provider_data in providers.items():
        if not isinstance(provider_data, dict):
            # File metadata such as `$schema` is not a provider.
            continue
        endpoint = provider_probe_endpoint(provider_data=provider_data)
        endpoint_to_providers.setdefault(f"{endpoint[0]}:{endpoint[1]}", []).append(name)

    with _probe_pod(client=client, targets=list(endpoint_to_providers)) as pod:
        output = pod.execute(
            command=["python3", "-c", _PROBE_SCRIPT, *endpoint_to_providers],
            timeout=PROBE_SCRIPT_TIMEOUT_SECONDS,
        )
    endpoint_rtts: dict[str, float | None] = json.loads(_last_json_line(output=output))
    return {name: endpoint_rtts.get(target) for target, names in endpoint_to_providers.items() for name in names}


def store_provider_selection(selection_path: Path, content: dict[str, Any]) -> None:
    """Atomically write the run's provider selection (or its recorded failure).

    The write goes to a sibling temporary file replaced into place, so xdist workers
    reading under the lock never observe a partially written selection.

    Args:
        selection_path (Path): Run-scoped selection file (`PYTEST_XDIST_TESTRUNUID`-keyed).
        content (dict[str, Any]): The selection, or `{"error": ...}` for a failed one.
    """
    tmp_path = selection_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(content))
    os.replace(tmp_path, selection_path)


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
    provider_version: str | None = None,
    required_feature: str | None = None,
) -> dict[str, Any]:
    """Derive the candidate pool, probe it, and select the fastest member.

    OpenShift pools are selected deterministically (lowest provider key) without probing:
    `create_source_provider` rewrites OpenShift sources to the local cluster host, so
    their configured endpoints are never used. For every other type, a pool whose
    members all share one probe endpoint — a single candidate, or several providers
    behind the same endpoint — is also selected without probing: no probe pod is
    created and the lowest provider key wins deterministically. Every non-OpenShift
    candidate's endpoint is resolved and validated before the shortcuts, so an entry
    with no resolvable host fails fast even when it is the pool's only member.

    Args:
        client (DynamicClient): OpenShift client used to run the probe pod.
        providers_json_path (str | None): Path to `.providers.json` (None resolves
            the standard location / `PROVIDERS_JSON_PATH` environment variable).
        provider_type (str): Required provider type (e.g. "vsphere").
        provider_version (str | None): Required provider version (e.g. "8.0.1");
            None pools every version of the type.
        required_feature (str | None): Keep only providers supporting this feature.

    Returns:
        dict[str, Any]: `{"provider": <name>, "rtt_ms": <median round-trip or None>}`.

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
    if provider_type == "openshift":
        # OpenShift sources always migrate through the local cluster: create_source_provider
        # rewrites their api_url to the destination cluster host, so their configured
        # endpoints are never used and probing them would rank an unused URL.
        selected = min(pool)
        LOGGER.info(
            f"OpenShift candidate providers use the local cluster endpoint, selected '{selected}' without probing"
        )
        return {"provider": selected, "rtt_ms": None}
    endpoints = {provider_probe_endpoint(provider_data=provider_data) for provider_data in pool.values()}
    if len(pool) == 1:
        selected = next(iter(pool))
        LOGGER.info(f"Single candidate provider '{selected}', selected without probing")
        return {"provider": selected, "rtt_ms": None}
    if len(endpoints) == 1:
        selected = min(pool)
        LOGGER.info(f"{len(pool)} candidate providers share one probe endpoint, selected '{selected}' without probing")
        return {"provider": selected, "rtt_ms": None}
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
        containers=[
            {
                "name": "probe",
                "image": PROBE_IMAGE,
                "command": ["sleep", str(PROBE_POD_IDLE_SECONDS)],
            }
        ],
    )
    try:
        namespace.deploy()
        # Wait for the namespace to be Active before deploying the pod: on OpenShift the
        # default ServiceAccount is created asynchronously, and a pod created in that
        # window can be rejected by admission (utilities/resources.py waits the same way).
        namespace.wait_for_status(status=Namespace.Status.ACTIVE)
        pod.deploy()
        pod.wait_for_status(status=Pod.Status.RUNNING, timeout=PROBE_POD_START_TIMEOUT_SECONDS)
        yield pod
    finally:
        try:
            pod.clean_up(wait=False)
        finally:
            namespace.clean_up(wait=True)


__all__ = [
    "PROBE_IMAGE",
    "derive_provider_pool",
    "probe_provider_latencies",
    "provider_feature_supported",
    "provider_probe_endpoint",
    "select_fastest_provider",
    "select_provider_from_pool",
    "store_provider_selection",
]
