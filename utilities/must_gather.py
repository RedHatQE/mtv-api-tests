from __future__ import annotations

import shlex
from pathlib import Path
from subprocess import CalledProcessError, TimeoutExpired
from typing import TYPE_CHECKING, cast
from urllib.parse import quote

import pytest
from kubernetes.dynamic.exceptions import DynamicApiError, ResourceNotFoundError
from ocp_resources.cluster_service_version import ClusterServiceVersion
from ocp_resources.image_digest_mirror_set import ImageDigestMirrorSet
from ocp_resources.exceptions import ClientWithBasicAuthError
from ocp_resources.plan import Plan
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

if TYPE_CHECKING:
    from _pytest.python import CallSpec2
    from kubernetes.dynamic import DynamicClient

LOGGER = get_logger(__name__)


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
        MustGatherImageError: If mirror entries or sources are missing, no entry contains
            ``must-gather`` in its source, or the matching entry has an empty mirrors list.
    """
    mirror_entries = idms.instance.spec.imageDigestMirrors
    if not mirror_entries:
        raise MustGatherImageError(f"IDMS '{idms.name}' has no imageDigestMirrors")
    for mirror_entry in mirror_entries:
        source = mirror_entry.get("source")
        if not source:
            raise MustGatherImageError(f"IDMS '{idms.name}' has a mirror entry without a source")
        if "must-gather" in source:
            mirrors = mirror_entry.get("mirrors", [])
            if not mirrors:
                raise MustGatherImageError(f"IDMS '{idms.name}' has must-gather entry with no mirrors")
            quay_mirrors = [m for m in mirrors if "quay" in m]
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
            MUST_GATHER_IMAGE value are missing from the CSV.
    """
    deployments = mtv_csv.instance.spec.install.spec.deployments
    if not deployments:
        raise MustGatherImageError(f"MTV ClusterServiceVersion '{mtv_csv.name}' has no deployments")
    containers = deployments[0].spec.template.spec.containers
    if not containers:
        raise MustGatherImageError(f"MTV ClusterServiceVersion '{mtv_csv.name}' has no containers")
    envs = containers[0].env
    if not envs:
        raise MustGatherImageError(f"MTV ClusterServiceVersion '{mtv_csv.name}' has no container env list")
    images = [env.get("value") for env in envs if env.get("name") == "MUST_GATHER_IMAGE"]
    if not images or not images[0]:
        raise MustGatherImageError(f"No MUST_GATHER_IMAGE value found in MTV ClusterServiceVersion '{mtv_csv.name}'")
    return images[0]


def _resolve_must_gather_image(
    ocp_admin_client: DynamicClient,
    mtv_subs: Subscription,
    mtv_csv: ClusterServiceVersion,
) -> str:
    """Resolve the must-gather image via IDMS.

    Extracts the must-gather image from the CSV, builds the IDMS resource name
    from the Subscription channel, retrieves the mirror URL, extracts the SHA
    from the CSV image, and combines them.

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
    channel = mtv_subs.instance.spec.channel
    idms_name = _get_idms_name(channel=channel)
    LOGGER.info(f"Looking up IDMS '{idms_name}' for must-gather mirror")

    idms = ImageDigestMirrorSet(client=ocp_admin_client, name=idms_name, ensure_exists=True)
    must_gather_mirror_url = _get_must_gather_mirror_url(idms=idms)

    if "@" not in csv_image:
        raise MustGatherImageError(f"CSV image '{csv_image}' does not contain a digest separator '@'")
    sha = csv_image.split("@")[1]
    resolved_image = f"{must_gather_mirror_url}@{sha}"
    LOGGER.info(f"Resolved must-gather image from IDMS: {resolved_image}")
    return resolved_image


def run_must_gather(data_collector_path: Path) -> bool:
    """Run ``oc adm must-gather`` to collect the full MTV diagnostic bundle.

    Used where there is no plan to scope by: session teardown after leftovers, and an item whose
    test class carries no ``plan_resource``. Resolves the must-gather image by looking up the IDMS
    mirror URL and combining it with the SHA from the installed CSV. Known operational failures
    return False for retry; programming and configuration errors propagate.

    Args:
        data_collector_path (Path): Directory where must-gather output is written.

    Returns:
        bool: True when the gather command ran. False when it did not, so the caller can keep the
            failure pending instead of recording a gather that produced no diagnostics.
    """
    LOGGER.info("Running full must-gather collection")
    return _run_must_gather(data_collector_path=data_collector_path, target_args="")


def run_plan_must_gather(data_collector_path: Path, plan: dict[str, str]) -> bool:
    """Run the plan-targeted ``oc adm must-gather`` collection for one migration plan.

    A different gather script from :func:`run_must_gather`: it runs ``/usr/bin/targeted`` with the
    plan's name and namespace as env, so the artifact holds that plan's VMs, networks and events
    instead of a full-cluster snapshot. Image resolution is shared with the full collection.

    Args:
        data_collector_path (Path): Directory where must-gather output is written.
        plan (dict[str, str]): Dict with ``name`` and ``namespace`` keys identifying the plan.

    Returns:
        bool: True when the gather command ran. False when it did not, so the caller can keep the
            failure pending instead of recording a gather that produced no diagnostics.
    """
    LOGGER.info(f"Running targeted must-gather for plan '{plan['name']}' in namespace '{plan['namespace']}'")
    target_args = f"NS={plan['namespace']} PLAN={plan['name']} /usr/bin/targeted"
    return _run_must_gather(data_collector_path=data_collector_path, target_args=target_args)


def _run_must_gather(data_collector_path: Path, target_args: str) -> bool:
    """Resolve the must-gather image and run ``oc adm must-gather`` with the given trailing arguments.

    Args:
        data_collector_path (Path): Directory where must-gather output is written.
        target_args (str): Arguments appended after the image and dest-dir flags, naming the gather
            script to run. Empty runs the full diagnostic collection.

    Returns:
        bool: True when the gather command ran. False when it did not.
    """
    try:
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

        # Invalid image metadata is configuration, not a retryable resource failure.
        must_gather_image = _resolve_must_gather_image(
            ocp_admin_client=ocp_admin_client,
            mtv_subs=mtv_subs,
            mtv_csv=mtv_csv,
        )

        command = f"oc adm must-gather --image={must_gather_image} --dest-dir={data_collector_path}"
        if target_args:
            command = f"{command} -- {target_args}"
        success, _, _ = run_command(shlex.split(command), verify_stderr=False)
        return success
    except NotImplementedError as ex:
        # The wrapper has no dedicated missing-API exception; match its discovery origin and message.
        origin = ex.__traceback__
        while origin is not None and origin.tb_next is not None:
            origin = origin.tb_next
        if (
            origin is None
            or origin.tb_frame.f_globals.get("__name__") != "ocp_resources.resource"
            or origin.tb_frame.f_code.co_name != "_get_api_version"
        ):
            raise
        if str(ex) not in {
            f"Couldn't find {resource.kind} in {resource.api_group} api group"
            for resource in (Subscription, ClusterServiceVersion, ImageDigestMirrorSet)
        }:
            raise
        LOGGER.exception(f"Must-gather resource API is unavailable. {ex}")
        return False
    except (
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
        LOGGER.exception(f"Failed to run must-gather. {ex}")
        return False


def collect_must_gather_for_item(node: pytest.Item, artifact_name: str, plan_obj: Plan | None) -> bool:
    """Collect a must-gather for a failure into a named directory under the data collector path.

    Args:
        node (pytest.Item): Test item supplying the data collector configuration.
        artifact_name (str): Worker-qualified class-plan identity, or standalone test node ID.
        plan_obj (Plan | None): Bound Plan context, or None for full collection.

    Returns:
        bool: True when a must-gather was collected, False when collection itself failed.
    """
    # Percent encoding is reversible, unlike replacing punctuation with dashes.
    data_collector_path = Path(node.session.config.getoption("data_collector_path")) / quote(artifact_name, safe="")
    if plan_obj is None:
        return run_must_gather(data_collector_path=data_collector_path)

    plan = {"name": cast("str", plan_obj.name), "namespace": cast("str", plan_obj.namespace)}
    return run_plan_must_gather(data_collector_path=data_collector_path, plan=plan)


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


def initialize_class_plan_context(item: pytest.Item) -> None:
    """Clear stale Plan state on entry, without resetting consecutive methods of the same plan.

    Args:
        item (pytest.Item): Item whose protocol is starting, before any setup hooks run.
    """
    identity = class_plan_identity(item)
    active_identity: str | None = getattr(item.session, "_must_gather_active_class_identity", None)
    if identity != active_identity:
        if identity is not None:
            node_cls: type = getattr(item, "cls")
            setattr(node_cls, "plan_resource", None)
        setattr(item.session, "_must_gather_active_class_identity", identity)


def _bind_class_plan_context(session: pytest.Session, identity: str, item: pytest.Item) -> Plan | None:
    """Retain the current Plan or explicit None for this identity's collection and retries.

    Args:
        session (pytest.Session): Session holding worker-local contexts.
        identity (str): Class-plan identity to bind if not already bound.
        item (pytest.Item): Item whose class may still hold this identity's Plan.

    Returns:
        Plan | None: First bound context, never a later class-plan's mutable attribute.
    """
    contexts: dict[str, Plan | None] = getattr(session, "_must_gather_plan_contexts", {})
    if identity not in contexts:
        active_identity: str | None = getattr(session, "_must_gather_active_class_identity", None)
        node_cls: type | None = getattr(item, "cls", None)
        contexts[identity] = getattr(node_cls, "plan_resource", None) if identity == active_identity else None
        setattr(session, "_must_gather_plan_contexts", contexts)
    return contexts[identity]


def mark_class_pending_must_gather(item: pytest.Item) -> None:
    """Store a failing item in session pending state for its class-plan instance.

    Args:
        item (pytest.Item): Failing item retained for plan context and the artifact path.
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
        item (pytest.Item): Item supplying configuration and context if not yet bound.

    Returns:
        bool: True when this call collected diagnostics and removed the pending entry.
    """
    pending: dict[str, pytest.Item] = getattr(session, "_must_gather_pending_classes", {})
    if identity not in pending or class_must_gather_collected(session, identity):
        return False

    plan_obj = _bind_class_plan_context(session, identity, item)
    if collect_must_gather_for_item(item, identity, plan_obj):
        pending.pop(identity, None)
        mark_class_must_gather_collected(session, identity)
        return True
    return False


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
        # Bind even without a pending failure: boundary finalizers may report one later.
        _bind_class_plan_context(item.session, identity, item)
        _collect_pending_class_must_gather(item.session, identity, item)


def collect_class_teardown_must_gather(node: pytest.Item) -> bool:
    """Queue a teardown failure, gathering now only if its class-plan boundary already passed.

    Exception interaction runs after fixture finalization. Earlier method teardown failures stay
    pending until the boundary; a boundary-only failure uses the pre-finalizer Plan context
    but can only collect post-finalizer cluster state.
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
    An unbound identity can use the class attribute only while it is still active; otherwise
    it binds None rather than targeting another plan's stale attribute.

    Args:
        session (pytest.Session): Session whose remaining pending map is consumed on success.
    """
    pending: dict[str, pytest.Item] = getattr(session, "_must_gather_pending_classes", {})
    for identity, item in list(pending.items()):
        _collect_pending_class_must_gather(session, identity, item)
