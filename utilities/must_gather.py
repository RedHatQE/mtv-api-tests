from __future__ import annotations

import shlex
from datetime import datetime, timezone
from pathlib import Path
from subprocess import CalledProcessError, TimeoutExpired
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import pytest
from kubernetes.dynamic.exceptions import ApiException, DynamicApiError, ResourceNotFoundError
from ocp_resources.cluster_service_version import ClusterServiceVersion
from ocp_resources.image_digest_mirror_set import ImageDigestMirrorSet
from ocp_resources.exceptions import ClientWithBasicAuthError
from ocp_resources.resource import Resource
from ocp_resources.subscription import Subscription
from pyhelper_utils.shell import run_command
from pytest_testconfig import py_config
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import HTTPError as RequestsHTTPError
from requests.exceptions import JSONDecodeError as RequestsJSONDecodeError
from requests.exceptions import Timeout as RequestsTimeout
from simple_logger.logger import get_logger
from timeout_sampler import TimeoutExpiredError
from urllib3.exceptions import MaxRetryError, ProtocolError, SSLError
from urllib3.exceptions import TimeoutError as HTTPTimeoutError

from exceptions.exceptions import MustGatherImageError
from utilities.constants import MTV_OPERATOR_NAME
from utilities.utils import get_cluster_client
from utilities.worker_node_selection import get_ready_schedulable_worker_nodes

if TYPE_CHECKING:
    from _pytest.python import CallSpec2
    from kubernetes.dynamic import DynamicClient

LOGGER = get_logger(__name__)

# The client aborts must-gather when the node filesystem backing the gather pod's emptyDir
# passes this percentage; time-scoped bundles are a few MB, so the 70 default is too low.
_MUST_GATHER_VOLUME_PERCENTAGE = 90


def _get_idms_name(channel: str) -> str:
    """Convert a Subscription channel to an IDMS name.

    Strips the ``release-v`` prefix (if present), replaces dots with dashes,
    and prepends ``devel-testing-for-``. Non-release channels like
    ``dev-preview`` are used as-is after the prefix.

    Args:
        channel (str): The Subscription channel string
            (e.g. ``release-v2.11``, ``dev-preview``).

    Returns:
        str: The derived IDMS resource name.

    Raises:
        MustGatherImageError: If ``channel`` is empty or has no version after
            the ``release-v`` prefix.
    """
    if not channel:
        raise MustGatherImageError("Subscription channel is empty")

    stripped = channel.removeprefix("release-v")
    if not stripped:
        raise MustGatherImageError(f"Subscription channel '{channel}' has no version after 'release-v'")
    return f"devel-testing-for-{stripped.replace('.', '-')}"


# The dict value type below is `Any` on purpose: provider-controlled metadata entries carry
# arbitrary value types until each caller validates the specific fields it reads.
def _entry_to_dict(entry: object, resource_description: str, entry_description: str) -> dict[str, Any]:
    """Normalize a provider-controlled metadata entry to a native dict for validation.

    Kubernetes dynamic API objects expose nested mappings as ``ResourceField`` objects that
    support ``.get()`` but are not ``dict`` instances, so valid live-cluster data fails an
    ``isinstance(entry, dict)`` check. ``ResourceField.to_dict()`` converts them. Importing
    ``ResourceField`` for an ``isinstance`` check is forbidden (no runtime ``kubernetes.*``
    imports beyond ``kubernetes.dynamic.exceptions``), so the conversion is detected duck-typed
    and the result is still type-checked.

    Args:
        entry (object): A metadata entry read from a provider-controlled resource.
        resource_description (str): Resource name and kind for error messages.
        entry_description (str): Short description of the entry kind for error messages.

    Returns:
        dict[str, Any]: The entry converted to a native dict.

    Raises:
        MustGatherImageError: If the entry cannot be converted to a mapping.
    """
    to_dict = getattr(entry, "to_dict", None)
    if callable(to_dict):
        entry = to_dict()
    if not isinstance(entry, dict):
        raise MustGatherImageError(f"{resource_description} has {entry_description} that is not a mapping: {entry!r}")
    return entry


def _get_must_gather_mirror_url(idms: ImageDigestMirrorSet) -> str:
    """Extract the must-gather mirror URL from an ImageDigestMirrorSet.

    Iterates over ``imageDigestMirrors`` entries and returns the mirror URL
    from the entry whose ``source`` contains ``must-gather``. Prefers a mirror
    containing ``quay`` in the URL; falls back to the first mirror otherwise.

    Args:
        idms (ImageDigestMirrorSet): The IDMS resource to inspect.

    Returns:
        str: The preferred quay mirror URL, or the first mirror URL if no
            quay mirror exists.

    Raises:
        MustGatherImageError: If mirror entries or sources are missing or malformed, no entry contains
            ``must-gather`` in its source, or the matching entry has an empty or invalid mirrors list.
    """
    mirror_entries = idms.instance.spec.imageDigestMirrors
    if not isinstance(mirror_entries, list) or not mirror_entries:
        raise MustGatherImageError(f"IDMS '{idms.name}' has no imageDigestMirrors")
    for mirror_entry in mirror_entries:
        entry = _entry_to_dict(mirror_entry, f"IDMS '{idms.name}'", "a mirror entry")
        source = entry.get("source")
        if not isinstance(source, str) or not source:
            raise MustGatherImageError(f"IDMS '{idms.name}' has a mirror entry with an invalid source: {source!r}")
        if "must-gather" in source:
            mirrors = entry.get("mirrors", [])
            if not isinstance(mirrors, list) or not mirrors:
                raise MustGatherImageError(f"IDMS '{idms.name}' has must-gather entry with no mirrors")
            invalid_mirrors = [mirror for mirror in mirrors if not isinstance(mirror, str) or not mirror]
            if invalid_mirrors:
                raise MustGatherImageError(
                    f"IDMS '{idms.name}' has mirror entries that are not non-empty strings: {invalid_mirrors!r}"
                )
            quay_mirrors = [mirror for mirror in mirrors if "quay" in mirror]
            return quay_mirrors[0] if quay_mirrors else mirrors[0]

    raise MustGatherImageError(f"No must-gather entry found in IDMS '{idms.name}'")


def _get_csv_must_gather_image(mtv_csv: ClusterServiceVersion) -> str:
    """Extract the MUST_GATHER_IMAGE value from the MTV CSV.

    Args:
        mtv_csv (ClusterServiceVersion): The MTV ClusterServiceVersion resource.

    Returns:
        str: The MUST_GATHER_IMAGE value.

    Raises:
        MustGatherImageError: If deployments, containers, environment variables or the
            MUST_GATHER_IMAGE value are missing or malformed in the CSV.
    """
    deployments = mtv_csv.instance.spec.install.spec.deployments
    if not deployments:
        raise MustGatherImageError(f"MTV ClusterServiceVersion '{mtv_csv.name}' has no deployments")
    containers = deployments[0].spec.template.spec.containers
    if not containers:
        raise MustGatherImageError(f"MTV ClusterServiceVersion '{mtv_csv.name}' has no containers")
    envs = containers[0].env
    if not isinstance(envs, list) or not envs:
        raise MustGatherImageError(f"MTV ClusterServiceVersion '{mtv_csv.name}' has no container env list")
    must_gather_envs: list[dict[str, str]] = []
    for env in envs:
        entry = _entry_to_dict(env, f"MTV ClusterServiceVersion '{mtv_csv.name}'", "an env entry")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise MustGatherImageError(
                f"MTV ClusterServiceVersion '{mtv_csv.name}' has an env entry with an invalid name: {name!r}"
            )
        if name == "MUST_GATHER_IMAGE":
            must_gather_envs.append(entry)
    if not must_gather_envs:
        raise MustGatherImageError(f"No MUST_GATHER_IMAGE value found in MTV ClusterServiceVersion '{mtv_csv.name}'")
    image = must_gather_envs[0].get("value")
    if not isinstance(image, str) or not image:
        raise MustGatherImageError(
            f"MUST_GATHER_IMAGE in MTV ClusterServiceVersion '{mtv_csv.name}' is not a non-empty string: {image!r}"
        )
    return image


def _is_missing_resource_api(ex: NotImplementedError, resource: type[Resource]) -> bool:
    """Check whether a NotImplementedError is the wrapper's missing-API error for one resource.

    The ``ocp_resources`` wrapper raises a bare ``NotImplementedError`` from
    ``_get_api_version`` when a resource kind is absent from the cluster's API
    discovery, with no dedicated exception type; the raise origin and message
    are the only discriminators.

    Args:
        ex (NotImplementedError): The exception to classify.
        resource (type[Resource]): Resource class the caller attempted to reach.

    Returns:
        bool: True when the exception is the wrapper's missing-API error for ``resource``.
    """
    origin = ex.__traceback__
    while origin is not None and origin.tb_next is not None:
        origin = origin.tb_next
    if (
        origin is None
        or origin.tb_frame.f_globals.get("__name__") != "ocp_resources.resource"
        or origin.tb_frame.f_code.co_name != "_get_api_version"
    ):
        return False
    return str(ex) == f"Couldn't find {resource.kind} in {resource.api_group} api group"


def _resolve_must_gather_image(
    ocp_admin_client: DynamicClient,
    mtv_subs: Subscription,
    mtv_csv: ClusterServiceVersion,
) -> str:
    """Resolve the MTV must-gather image, using the channel-derived IDMS as an optional mirror.

    The image is always the MTV one from the CSV's ``MUST_GATHER_IMAGE``: the Forklift
    collection scripts are a feature of that image only, so no other must-gather image
    may ever be substituted. When the channel-derived IDMS exists, its mirror URL replaces
    the registry prefix and the CSV digest is kept (needed on devel clusters without direct
    ``registry.redhat.io`` access). When the IDMS resource is absent (GA/brew clusters) or its
    API is unavailable (older OpenShift), the fully-qualified CSV image is returned verbatim —
    any cluster with direct registry access can pull it.

    Args:
        ocp_admin_client (DynamicClient): The OpenShift admin client.
        mtv_subs (Subscription): The MTV operator Subscription resource.
        mtv_csv (ClusterServiceVersion): The MTV ClusterServiceVersion resource
            (used to extract the must-gather image for SHA extraction).

    Returns:
        str: The resolved must-gather image string.

    Raises:
        MustGatherImageError: If MUST_GATHER_IMAGE is missing from the CSV environment
            variables, the Subscription channel is empty, no must-gather entry
            is found in the IDMS, or the CSV image has no digest separator.
    """
    csv_image = _get_csv_must_gather_image(mtv_csv=mtv_csv)
    if "@" not in csv_image:
        raise MustGatherImageError(f"CSV image '{csv_image}' does not contain a digest separator '@'")
    sha = csv_image.split("@")[1]
    if not sha:
        raise MustGatherImageError(f"CSV image '{csv_image}' has an empty digest after '@'")
    channel = mtv_subs.instance.spec.channel
    idms_name = _get_idms_name(channel=channel)
    LOGGER.info(f"Looking up IDMS '{idms_name}' for must-gather mirror")

    try:
        idms = ImageDigestMirrorSet(client=ocp_admin_client, name=idms_name, ensure_exists=True)
    except (ResourceNotFoundError, NotImplementedError) as ex:
        if isinstance(ex, NotImplementedError) and not _is_missing_resource_api(ex, ImageDigestMirrorSet):
            raise
        # Optional accelerator: without the channel-derived IDMS, the fully-qualified CSV image
        # is pulled directly; a pull failure stays a retryable command error.
        LOGGER.info(
            f"IDMS '{idms_name}' unavailable on this cluster ({ex}); "
            f"using the CSV must-gather image directly: {csv_image}"
        )
        return csv_image

    must_gather_mirror_url = _get_must_gather_mirror_url(idms=idms)
    resolved_image = f"{must_gather_mirror_url}@{sha}"
    LOGGER.info(f"Resolved must-gather image from IDMS: {resolved_image}")
    return resolved_image


def _utc_rfc3339_now() -> str:
    """Return the current UTC time as an RFC 3339 timestamp for ``--since-time``.

    Returns:
        str: The current UTC time, e.g. ``2026-11-07T09:15:00Z``.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record_session_start_time(session: pytest.Session) -> None:
    """Record the session start timestamp scoping standalone and teardown gathers.

    Args:
        session (pytest.Session): Session to record the start time on.
    """
    setattr(session, "_must_gather_session_start_time", _utc_rfc3339_now())


def session_start_time(session: pytest.Session) -> str:
    """Return the session start timestamp recorded at session start.

    Args:
        session (pytest.Session): Session holding the start timestamp.

    Returns:
        str: The UTC RFC 3339 session start timestamp.
    """
    return session._must_gather_session_start_time


def _bundle_has_diagnostics(data_collector_path: Path) -> bool:
    """Report whether a must-gather destination holds any collected diagnostics.

    Args:
        data_collector_path (Path): Directory the gather wrote to.

    Returns:
        bool: True when any file exists under a bundle's ``namespaces/`` tree, excluding the
            client's fallback ``inspect.local.*`` collections.
    """
    return any(
        path.is_file()
        for path in data_collector_path.glob("*/namespaces/**/*")
        if not path.relative_to(data_collector_path).parts[0].startswith("inspect.local.")
    )


def _gather_with_node_retry(
    data_collector_path: Path,
    must_gather_image: str,
    since_time: str,
    ocp_admin_client: DynamicClient,
) -> bool:
    """Run the full gather, retrying pinned to worker nodes while the destination stays empty.

    The disk-usage guard kills gathers on nodes above the raised ``--volume-percentage`` while
    exiting 0, and a partially failed gather can exit 1 with a usable bundle, so acceptance is
    based on the diagnostics in the destination — which accumulates across attempts — rather
    than the exit code. While it holds none, retry pinned to each Ready schedulable worker;
    clusters without workers keep only the default scheduling.

    Args:
        data_collector_path (Path): Directory the gather writes to.
        must_gather_image (str): Resolved must-gather container image.
        since_time (str): UTC RFC 3339 timestamp bounding collected logs.
        ocp_admin_client (DynamicClient): OpenShift client for listing worker nodes.

    Returns:
        bool: True when the destination holds diagnostics after an attempt. False when it still
            holds none, so the caller can keep the failure pending instead of recording an
            empty gather.
    """
    command_args = (
        f"--volume-percentage={_MUST_GATHER_VOLUME_PERCENTAGE} "
        f"--image={must_gather_image} --dest-dir={data_collector_path} "
        f"-- MUST_GATHER_SINCE_TIME={since_time} /usr/bin/gather"
    )
    run_command(shlex.split(f"oc adm must-gather {command_args}"), verify_stderr=False, check=False)
    if _bundle_has_diagnostics(data_collector_path):
        return True

    worker_nodes = [node.name for node in get_ready_schedulable_worker_nodes(ocp_client=ocp_admin_client)]
    for node_name in worker_nodes:
        LOGGER.warning(f"Must-gather attempt left no diagnostics; retrying pinned to worker node '{node_name}'")
        run_command(
            shlex.split(f"oc adm must-gather --node-name={node_name} {command_args}"), verify_stderr=False, check=False
        )
        if _bundle_has_diagnostics(data_collector_path):
            return True
    return False


def run_must_gather(data_collector_path: Path, since_time: str) -> bool:
    """Run ``oc adm must-gather`` collecting all logs since ``since_time``.

    Runs the full Forklift collection (``/usr/bin/gather``) with ``MUST_GATHER_SINCE_TIME``
    bounding every pod-log query to the given RFC 3339 timestamp, so the bundle holds the
    cluster's Forklift-relevant logs and CRs for the failing test's execution window — all pods
    in the Forklift namespaces (current and previous logs), CDI and vm-import component logs,
    current virt-launcher logs cluster-wide, and every plan's migration pods, none of it
    grep-filtered the way the plan-targeted script was. Resolves the must-gather image from the
    installed MTV operator CSV, using the channel-derived IDMS as an optional mirror; when that
    IDMS is absent or its API is unavailable, the CSV image is used verbatim. The client's
    disk-usage guard is raised to 90% (time-scoped bundles are a few MB), attempts are validated
    by the diagnostics they produced, and empty outcomes are retried pinned to worker nodes.
    Known operational failures return False for retry; programming and configuration errors
    propagate.

    Args:
        data_collector_path (Path): Directory where must-gather output is written.
        since_time (str): UTC RFC 3339 timestamp; logs older than it are not collected.

    Returns:
        bool: True when an attempt ran and its bundle holds diagnostics. False when none did,
            so the caller can keep the failure pending instead of recording an empty gather.
    """
    LOGGER.info(f"Running full must-gather collection with logs since {since_time}")
    try:
        # Resource discovery: cluster client, MTV Subscription, and its installed CSV.
        # https://github.com/kubev2v/forklift-must-gather
        ocp_admin_client = get_cluster_client()
        mtv_namespace = py_config["mtv_namespace"]
        mtv_subs = Subscription(
            client=ocp_admin_client, name=MTV_OPERATOR_NAME, namespace=mtv_namespace, ensure_exists=True
        )

        installed_csv = mtv_subs.instance.status.installedCSV
        mtv_csv = ClusterServiceVersion(
            client=ocp_admin_client, name=installed_csv, namespace=mtv_namespace, ensure_exists=True
        )

        # Image resolution: derive the gather image from operator state. Invalid image metadata
        # is configuration, not a retryable resource failure.
        must_gather_image = _resolve_must_gather_image(
            ocp_admin_client=ocp_admin_client,
            mtv_subs=mtv_subs,
            mtv_csv=mtv_csv,
        )

        # Command execution: the guard still exits 0 after killing a gather, so attempts are
        # validated by their diagnostics and retried pinned to worker nodes.
        return _gather_with_node_retry(
            data_collector_path=data_collector_path,
            must_gather_image=must_gather_image,
            since_time=since_time,
            ocp_admin_client=ocp_admin_client,
        )
    except NotImplementedError as ex:
        # API unavailability: the wrapper has no dedicated missing-API exception, so match its
        # discovery origin and message before treating it as a gather that cannot run. Only the
        # Subscription and CSV kinds remain here — IDMS API-unavailability is handled inside
        # image resolution as a fallback to the CSV image, not a gather that cannot run.
        if not any(_is_missing_resource_api(ex, resource) for resource in (Subscription, ClusterServiceVersion)):
            raise
        LOGGER.exception(f"Must-gather resource API is unavailable, no bundle in '{data_collector_path}'. {ex}")
        return False
    except (
        ApiException,
        CalledProcessError,
        TimeoutExpired,
        OSError,
        ClientWithBasicAuthError,
        DynamicApiError,
        ResourceNotFoundError,
        RequestsConnectionError,
        RequestsHTTPError,
        RequestsJSONDecodeError,
        RequestsTimeout,
        MaxRetryError,
        ProtocolError,
        SSLError,
        HTTPTimeoutError,
        TimeoutExpiredError,
    ) as ex:
        # Retryable operational failures: log and report the gather as not run; the caller keeps
        # the class-plan identity pending for the session-end retry.
        LOGGER.exception(f"Failed to run must-gather, no bundle in '{data_collector_path}'. {ex}")
        return False


def collect_must_gather_for_item(node: pytest.Item, artifact_name: str, since_time: str) -> bool:
    """Collect a must-gather for a failure into a named directory under the data collector path.

    Args:
        node (pytest.Item): Test item supplying the data collector configuration.
        artifact_name (str): Worker-qualified class-plan identity, or standalone test node ID.
        since_time (str): UTC RFC 3339 timestamp bounding the collected log window.

    Returns:
        bool: True when a must-gather was collected, False when collection itself failed.
    """
    # Percent encoding is reversible, unlike replacing punctuation with dashes.
    data_collector_path = Path(node.session.config.getoption("data_collector_path")) / quote(artifact_name, safe="")
    gathered = run_must_gather(data_collector_path=data_collector_path, since_time=since_time)

    if not gathered:
        LOGGER.warning(f"No must-gather bundle for '{artifact_name}' in '{data_collector_path}'")
    return gathered


def class_plan_identity(item: pytest.Item | None) -> str | None:
    """Identify a class-plan instance independently of its test methods.

    Pytest keeps one Class collector across parameter sets. Callspec indices for class-scoped
    parameters distinguish fixture instances without including function-scoped method parameters.
    Worker IDs keep artifacts separate when xdist splits a class across workers.

    Args:
        item (pytest.Item | None): Current or next item, or None at the end of execution.

    Returns:
        str | None: Worker-qualified class node ID and class parameter indices, or None for no class.
    """
    if item is None:
        return None
    class_node = item.getparent(pytest.Class)
    if class_node is None:
        return None

    callspec: CallSpec2 | None = getattr(item, "callspec", None)
    parameters = ""
    if callspec is not None:
        # Pytest exposes parameter scopes only on CallSpec2._arg2scope.
        parameters = ",".join(
            f"{name}={callspec.indices[name]}"
            for name, scope in sorted(callspec._arg2scope.items())
            if scope.value == "class" or name == "class_plan_config"
        )
    worker: str = getattr(item.config, "workerinput", {}).get("workerid", "master")
    return f"{class_node.nodeid}::params={parameters}::worker={worker}"


def record_class_plan_start_time(item: pytest.Item) -> None:
    """Record when this class-plan instance started, bounding its failure gather window.

    Runs from a ``tryfirst`` protocol wrapper before any setup hook, so the timestamp
    always precedes the first possible failure in the class. One timestamp per
    class-plan identity, first seen wins: xdist scheduling can split one class across
    non-contiguous batches, and the original start keeps the whole execution window.

    Args:
        item (pytest.Item): Item whose protocol is starting.
    """
    identity = class_plan_identity(item)
    if identity is None:
        return
    start_times: dict[str, str] = getattr(item.session, "_must_gather_class_start_times", {})
    start_times.setdefault(identity, _utc_rfc3339_now())
    setattr(item.session, "_must_gather_class_start_times", start_times)


def _class_plan_start_time(session: pytest.Session, identity: str) -> str:
    """Return the recorded start timestamp for a class-plan identity.

    Args:
        session (pytest.Session): Session holding worker-local start times.
        identity (str): Class-plan identity whose window start is needed.

    Returns:
        str: The UTC RFC 3339 timestamp recorded when the identity first started.

    Raises:
        KeyError: If the identity never started in this worker; a pending gather cannot
            exist without it because the protocol wrapper records the time first.
    """
    start_times: dict[str, str] = getattr(session, "_must_gather_class_start_times", {})
    return start_times[identity]


def mark_class_pending_must_gather(item: pytest.Item) -> None:
    """Store a failing item in session pending state for its class-plan instance.

    Args:
        item (pytest.Item): Failing item retained for the artifact path.
    """
    identity = class_plan_identity(item)
    if identity is None or class_must_gather_collected(item.session, identity):
        return
    pending: dict[str, pytest.Item] = getattr(item.session, "_must_gather_pending_classes", {})
    pending.setdefault(identity, item)
    setattr(item.session, "_must_gather_pending_classes", pending)


def class_must_gather_collected(session: pytest.Session, identity: str) -> bool:
    """Report whether this worker already gathered a class-plan instance successfully.

    Args:
        session (pytest.Session): Session holding worker-local state.
        identity (str): Class-plan identity to look up.

    Returns:
        bool: True only after a successful gather.
    """
    collected: set[str] = getattr(session, "_must_gather_collected_classes", set())
    return identity in collected


def mark_class_must_gather_collected(session: pytest.Session, identity: str) -> None:
    """Add a successfully gathered class-plan identity to session collected state.

    Args:
        session (pytest.Session): Session holding worker-local state.
        identity (str): Successfully gathered class-plan instance.
    """
    collected: set[str] = getattr(session, "_must_gather_collected_classes", set())
    collected.add(identity)
    setattr(session, "_must_gather_collected_classes", collected)


def _collect_pending_class_must_gather(session: pytest.Session, identity: str, item: pytest.Item) -> bool:
    """Collect an owed class-plan gather, updating session state only on success.

    A failed operational collection stays pending for the session-end retry.

    Args:
        session (pytest.Session): Session holding pending and collected state.
        identity (str): Class-plan instance owing diagnostics.
        item (pytest.Item): Item supplying configuration and the artifact path.

    Returns:
        bool: True when this call collected diagnostics and removed the pending entry.
    """
    pending: dict[str, pytest.Item] = getattr(session, "_must_gather_pending_classes", {})
    if identity not in pending or class_must_gather_collected(session, identity):
        return False

    since_time = _class_plan_start_time(session=session, identity=identity)
    if collect_must_gather_for_item(node=item, artifact_name=identity, since_time=since_time):
        pending.pop(identity, None)
        mark_class_must_gather_collected(session, identity)
        return True
    LOGGER.warning(f"Must-gather for class-plan '{identity}' produced no bundle, keeping it pending for the retry")
    return False


def collect_pending_class_must_gather_now(item: pytest.Item) -> None:
    """Collect a pending class-plan gather immediately instead of at the class-plan boundary.

    Used before a blocking post-mortem debugger session so diagnostics are gathered while the
    migration pods still exist, before fixture cleanup deletes them. Success marks the identity
    collected and the boundary skips it; a failed attempt stays pending for the boundary retry
    and the session-end flush.

    Args:
        item (pytest.Item): Failing item whose class-plan identity may owe a gather.
    """
    identity = class_plan_identity(item)
    if identity is None:
        return
    try:
        _collect_pending_class_must_gather(item.session, identity, item)
    except MustGatherImageError:
        # Keep the identity pending: the session-end flush reports the configuration error
        # without turning the calling hook into an INTERNALERROR.
        LOGGER.exception(f"Must-gather for class-plan '{identity}' failed before the remote debugger session")


def collect_class_must_gather(item: pytest.Item, nextitem: pytest.Item | None) -> None:
    """Gather pending diagnostics at a worker's class-plan boundary before fixture cleanup.

    Called by a tryfirst teardown hook. A different Class collector or class parameter set ends
    the current instance; None flushes it on normal completion or early -x/--maxfail exit.
    Parameter changes can finalize fixtures during the next setup even when the Class collector
    stays active, so they must also count as boundaries. The next identity is retained on the
    item so a later teardown exception can defer collection while this instance continues.

    Args:
        item (pytest.Item): Item about to tear down, including setup-error items.
        nextitem (pytest.Item | None): Next item scheduled on this worker, or None on exit.
    """
    identity = class_plan_identity(item)
    next_identity = class_plan_identity(nextitem)
    setattr(item, "_must_gather_next_class_identity", next_identity)
    if identity is not None and identity != next_identity:
        _collect_pending_class_must_gather(item.session, identity, item)


def collect_class_teardown_must_gather(node: pytest.Item) -> bool:
    """Queue a teardown failure, gathering now only if its class-plan boundary already passed.

    Exception interaction runs after fixture finalization. Earlier method teardown failures stay
    pending until the boundary; a boundary-only failure collects with this instance's start-time
    window but can only reach post-finalizer cluster state.
    A successful pre-finalizer gather suppresses this repeat.

    Args:
        node (pytest.Item): Item whose teardown failed.

    Returns:
        bool: True when this call collected diagnostics successfully.
    """
    identity = class_plan_identity(node)
    if identity is None:
        return False
    if class_must_gather_collected(node.session, identity):
        LOGGER.info(f"Suppressing must-gather for {identity}: class-plan must-gather already collected.")
        return False

    mark_class_pending_must_gather(node)
    if getattr(node, "_must_gather_next_class_identity", None) == identity:
        return False
    return _collect_pending_class_must_gather(node.session, identity, node)


def flush_pending_class_must_gathers(session: pytest.Session) -> None:
    """Retry remaining pending gathers at session finish if this worker is still alive.

    This fallback cannot guarantee pre-cleanup state after interrupted hooks or recover state
    from a crashed worker process. Normal early exits gather at the nextitem=None boundary.
    Every identity's retry runs even when an earlier one fails; the first configuration error
    is re-raised after the loop so one failure cannot skip the remaining diagnostics.

    Args:
        session (pytest.Session): Session whose remaining pending map is consumed on success.

    Raises:
        MustGatherImageError: The first configuration error encountered across the retries,
            re-raised with its traceback after every identity was attempted.
    """
    pending: dict[str, pytest.Item] = getattr(session, "_must_gather_pending_classes", {})
    retry_errors: list[MustGatherImageError] = []
    for identity, item in list(pending.items()):
        try:
            _collect_pending_class_must_gather(session, identity, item)
        except MustGatherImageError as exp:
            # One identity's configuration failure must not skip the remaining retries.
            LOGGER.exception(f"Must-gather retry for class-plan '{identity}' failed at session finish")
            retry_errors.append(exp)
    if retry_errors:
        raise retry_errors[0]
