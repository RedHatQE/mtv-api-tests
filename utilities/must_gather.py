from __future__ import annotations

import shlex
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from ocp_resources.cluster_service_version import ClusterServiceVersion
from ocp_resources.image_digest_mirror_set import ImageDigestMirrorSet
from ocp_resources.plan import Plan
from ocp_resources.subscription import Subscription
from pyhelper_utils.shell import run_command
from pytest_testconfig import py_config
from simple_logger.logger import get_logger

from utilities.constants import MTV_OPERATOR_NAME
from utilities.naming import sanitize_test_name_for_path
from utilities.utils import get_cluster_client

if TYPE_CHECKING:
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
        ValueError: If ``channel`` is empty or has no version after
            the ``release-v`` prefix.
    """
    if not channel:
        raise ValueError("Subscription channel is empty")

    stripped = channel.removeprefix("release-v")
    if not stripped:
        raise ValueError(f"Subscription channel '{channel}' has no version after 'release-v'")
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
        ValueError: If no ``imageDigestMirrors`` entry contains ``must-gather``
            in its source, or if the matching entry has an empty mirrors list.
    """
    for mirror_entry in idms.instance.spec.imageDigestMirrors:
        if "must-gather" in mirror_entry["source"]:
            mirrors = mirror_entry.get("mirrors", [])
            if not mirrors:
                raise ValueError(f"IDMS '{idms.name}' has must-gather entry with no mirrors")
            quay_mirrors = [m for m in mirrors if "quay" in m]
            return quay_mirrors[0] if quay_mirrors else mirrors[0]

    raise ValueError(f"No must-gather entry found in IDMS '{idms.name}'")


def _get_csv_must_gather_image(mtv_csv: ClusterServiceVersion) -> str:
    """Extract the MUST_GATHER_IMAGE value from the MTV CSV.

    Args:
        mtv_csv (ClusterServiceVersion): The MTV ClusterServiceVersion resource.

    Returns:
        str: The MUST_GATHER_IMAGE value.

    Raises:
        ValueError: If the container env list is None or MUST_GATHER_IMAGE is
            missing from the CSV environment variables.
    """
    envs = mtv_csv.instance.spec.install.spec.deployments[0].spec.template.spec.containers[0].env
    if envs is None:
        raise ValueError(f"MTV ClusterServiceVersion '{mtv_csv.name}' has no container env list")
    images = [env["value"] for env in envs if env["name"] == "MUST_GATHER_IMAGE"]
    if not images:
        raise ValueError(f"No MUST_GATHER_IMAGE found in MTV ClusterServiceVersion '{mtv_csv.name}'")
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
        ValueError: If MUST_GATHER_IMAGE is missing from the CSV environment
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
        raise ValueError(f"CSV image '{csv_image}' does not contain a digest separator '@'")
    sha = csv_image.split("@")[1]
    resolved_image = f"{must_gather_mirror_url}@{sha}"
    LOGGER.info(f"Resolved must-gather image from IDMS: {resolved_image}")
    return resolved_image


def run_must_gather(data_collector_path: Path, plan: dict[str, str] | None = None) -> bool:
    """Run ``oc adm must-gather`` to collect MTV diagnostic data.

    Resolves the must-gather image by looking up the IDMS mirror URL and
    combining it with the SHA from the installed CSV. Any errors during
    resolution are logged but do not fail the test run.

    Args:
        data_collector_path (Path): Directory where must-gather output is written.
        plan (dict[str, str] | None): Optional dict with ``name`` and ``namespace``
            keys to scope the must-gather to a specific migration plan.

    Returns:
        bool: True when the gather command ran. False when it did not, so the caller can keep the
            failure pending instead of recording a gather that produced no diagnostics.
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

        must_gather_image = _resolve_must_gather_image(
            ocp_admin_client=ocp_admin_client,
            mtv_subs=mtv_subs,
            mtv_csv=mtv_csv,
        )

        _must_gather_base_cmd = f"oc adm must-gather --image={must_gather_image} --dest-dir={data_collector_path}"

        if plan:
            plan_name = plan["name"]
            plan_namespace = plan["namespace"]
            LOGGER.info(f"Running targeted must-gather for plan '{plan_name}' in namespace '{plan_namespace}'")
            run_command(
                shlex.split(f"{_must_gather_base_cmd} -- NS={plan_namespace} PLAN={plan_name} /usr/bin/targeted"),
                verify_stderr=False,
            )
        else:
            LOGGER.info("Running full must-gather collection")
            run_command(shlex.split(_must_gather_base_cmd), verify_stderr=False)
        return True
    except Exception as ex:
        LOGGER.exception(f"Failed to run must-gather. {ex}")
        return False


def collect_must_gather_for_item(node: pytest.Item, artifact_name: str) -> bool:
    """Collect a must-gather for a failure into a named directory under the data collector path.

    Args:
        node (pytest.Item): The failing test item. Its class supplies the ``plan_resource`` context.
        artifact_name (str): Base name of the artifact directory. Class-scoped failures pass the class name,
            so every failure of that class lands in the same directory.

    Returns:
        bool: True when a must-gather was collected, False when collection itself failed.
    """
    data_collector_path = Path(
        f"{node.session.config.getoption('data_collector_path')}/{sanitize_test_name_for_path(artifact_name)}"
    )
    plan: dict[str, str] | None = None
    plan_obj: Plan | None = getattr(getattr(node, "cls", None), "plan_resource", None)
    if plan_obj:
        plan = {"name": cast("str", plan_obj.name), "namespace": cast("str", plan_obj.namespace)}

    return run_must_gather(data_collector_path=data_collector_path, plan=plan)


def mark_class_pending_must_gather(session: pytest.Session, cls: type, item: pytest.Item) -> None:
    """Record that a test class owes one must-gather, to be collected when the class ends.

    State lives on the session object, so under xdist every worker tracks only the classes it ran and no
    worker can suppress another worker's gather. The failing item is kept alongside the class because an
    interrupted run has no class end left to wait for, and the session-end flush needs an item for the
    plan context and the data collector path.

    Args:
        session (pytest.Session): The session running the failing item.
        cls (type): The test class whose failures must be covered by a single must-gather.
        item (pytest.Item): The failing item, used to collect the deferred gather later.
    """
    pending: dict[type, pytest.Item] = getattr(session, "_must_gather_pending_classes", {})
    pending.setdefault(cls, item)
    setattr(session, "_must_gather_pending_classes", pending)


def class_must_gather_collected(session: pytest.Session, cls: type) -> bool:
    """Report whether this class's one class-scoped must-gather was already collected.

    Args:
        session (pytest.Session): The session running the item.
        cls (type): The test class to look up.

    Returns:
        bool: True if a class-scoped must-gather was already collected for this class.
    """
    collected: set[type] = getattr(session, "_must_gather_collected_classes", set())
    return cls in collected


def mark_class_must_gather_collected(session: pytest.Session, cls: type) -> None:
    """Record that the class's one class-scoped must-gather has been collected.

    Args:
        session (pytest.Session): The session running the item.
        cls (type): The test class the must-gather was collected for.
    """
    collected: set[type] = getattr(session, "_must_gather_collected_classes", set())
    collected.add(cls)
    setattr(session, "_must_gather_collected_classes", collected)


def _class_item_counts(session: pytest.Session) -> dict[type, int]:
    """Count the collected items of every test class.

    The count is cached on the session and rebuilt whenever the number of collected items changes, so
    re-collection (``--lf``, xdist workerdist, plugins that add items) cannot leave it stale.

    Args:
        session (pytest.Session): The session whose collected items define the per-class totals.

    Returns:
        dict[type, int]: Number of collected items per test class.
    """
    cached_count: int | None
    cached_counts: dict[type, int]
    cached_count, cached_counts = getattr(session, "_must_gather_class_item_counts", (None, {}))
    if cached_count == len(session.items):
        return cached_counts

    counts: dict[type, int] = {}
    for session_item in session.items:
        item_cls: type | None = getattr(session_item, "cls", None)
        if item_cls is not None:
            counts[item_cls] = counts.get(item_cls, 0) + 1

    setattr(session, "_must_gather_class_item_counts", (len(session.items), counts))
    return counts


def _record_class_item_completed(session: pytest.Session, cls: type) -> bool:
    """Count a torn-down item for its class and report whether the class ended on this worker.

    Completion is counted per worker rather than taken from the collection-last item of the class: under
    ``--dist=load`` xdist spreads a class's methods across workers, so the collection-last item can run
    on a worker that never saw the failure and would never collect the pending gather.

    Args:
        session (pytest.Session): The session running the item.
        cls (type): The class of the item being torn down.

    Returns:
        bool: True when every collected item of this class completed on this worker.
    """
    completed: dict[type, int] = getattr(session, "_must_gather_class_items_completed", {})
    completed[cls] = completed.get(cls, 0) + 1
    setattr(session, "_must_gather_class_items_completed", completed)
    return completed[cls] >= _class_item_counts(session).get(cls, 0)


def _collect_pending_class_must_gather(session: pytest.Session, cls: type, item: pytest.Item) -> None:
    """Collect the must-gather a class owes, unless the class has no pending failure or already has one.

    The class is marked collected only after a gather actually ran, so a failed collection stays pending
    and is retried by the session-end flush instead of being lost.

    Args:
        session (pytest.Session): The session running the item.
        cls (type): The test class owing the must-gather.
        item (pytest.Item): The item used to collect the gather and to supply the plan context.
    """
    pending: dict[type, pytest.Item] = getattr(session, "_must_gather_pending_classes", {})
    if cls not in pending or class_must_gather_collected(session, cls):
        return

    if collect_must_gather_for_item(item, cls.__name__):
        pending.pop(cls, None)
        mark_class_must_gather_collected(session, cls)


def collect_class_must_gather(item: pytest.Item) -> None:
    """Collect the must-gather owed by the item's test class, once, when the class ends on this worker.

    Collection is deferred to the end of the class because a shared fixture failure errors or fails every
    method in the class while the cluster keeps changing under them: a must-gather taken at the first
    failure is already stale by the last one. One gather at class end captures the final state of all those
    failures in a single, class-scoped artifact directory.

    ``pytest_runtest_teardown`` runs before the class-scoped fixture finalizer, so this gather captures the
    pre-cleanup state - the plan, VMs and namespaces that were still present when the class failed. That is
    what makes it diagnostic. It also means a class fixture finalizer failure arrives after this gather, and
    is suppressed rather than repeated, because the class is marked collected here.

    The hook runs for every item, including items that errored in setup, which is what makes the
    setup-failure case reachable. It must not be guarded on the item passing or on a recorded failure,
    otherwise the class never gets its gather. A class that only partly ran on this worker (xdist
    ``--dist=load``) is left pending for ``flush_pending_class_must_gathers``.

    Args:
        item (pytest.Item): The item being torn down.
    """
    item_cls: type | None = getattr(item, "cls", None)
    if item_cls is None:
        return

    session: pytest.Session = item.session
    if not _record_class_item_completed(session, item_cls):
        return

    _collect_pending_class_must_gather(session, item_cls, item)


def collect_class_teardown_must_gather(node: pytest.Item, cls: type) -> bool:
    """Collect a must-gather for a class whose fixture teardown failed, unless the class already has one.

    ``pytest_exception_interact`` runs before the class fixture finalizer, so a class gathered at its own
    end already has artifacts covering the very resources that finalizer was tearing down. Collecting again
    would duplicate that gather moments later against near-identical state, so it is suppressed instead.

    Args:
        node (pytest.Item): The item whose teardown failed.
        cls (type): The test class the failing fixture belongs to.

    Returns:
        bool: True when a must-gather was collected by this call.
    """
    session: pytest.Session = node.session
    if class_must_gather_collected(session, cls):
        LOGGER.info(
            f"Suppressing must-gather for {cls.__name__}: class-end must-gather already collected "
            "before the fixture teardown failure."
        )
        return False

    if not collect_must_gather_for_item(node, cls.__name__):
        return False

    # Drop any pending failure state as well: this gather covers the whole class, so a later failure must
    # not produce a second gather into the same class-named directory.
    pending: dict[type, pytest.Item] = getattr(session, "_must_gather_pending_classes", {})
    pending.pop(cls, None)
    mark_class_must_gather_collected(session, cls)
    return True


def flush_pending_class_must_gathers(session: pytest.Session) -> None:
    """Collect the must-gathers still owed by classes whose end never ran on this worker.

    ``-x``, ``--maxfail``, Ctrl-C and a crashed worker all stop execution before the last item of a failed
    class tears down, which would silently drop that class's diagnostics. Classes still pending at session
    end are collected here, once each, into the same class-named directories the normal teardown path uses.

    Args:
        session (pytest.Session): The session that ran the failing items.
    """
    pending: dict[type, pytest.Item] = getattr(session, "_must_gather_pending_classes", {})
    for cls, item in list(pending.items()):
        _collect_pending_class_must_gather(session, cls, item)
