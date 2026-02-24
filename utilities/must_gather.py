from __future__ import annotations

import shlex
from pathlib import Path
from typing import TYPE_CHECKING

from ocp_resources.cluster_service_version import ClusterServiceVersion
from ocp_resources.image_digest_mirror_set import ImageDigestMirrorSet
from ocp_resources.subscription import Subscription
from pyhelper_utils.shell import run_command
from pytest_testconfig import py_config
from simple_logger.logger import get_logger

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


def run_must_gather(data_collector_path: Path, plan: dict[str, str] | None = None) -> None:
    """Run ``oc adm must-gather`` to collect MTV diagnostic data.

    Resolves the must-gather image by looking up the IDMS mirror URL and
    combining it with the SHA from the installed CSV. Any errors during
    resolution are logged but do not fail the test run.

    Args:
        data_collector_path (Path): Directory where must-gather output is written.
        plan (dict[str, str] | None): Optional dict with ``name`` and ``namespace``
            keys to scope the must-gather to a specific migration plan.
    """
    try:
        # https://github.com/kubev2v/forklift-must-gather
        ocp_admin_client = get_cluster_client()
        mtv_namespace = py_config["mtv_namespace"]
        mtv_subs = Subscription(
            client=ocp_admin_client, name="mtv-operator", namespace=mtv_namespace, ensure_exists=True
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
            run_command(
                shlex.split(f"{_must_gather_base_cmd} -- NS={plan_namespace} PLAN={plan_name} /usr/bin/targeted")
            )
        else:
            run_command(shlex.split(f"{_must_gather_base_cmd} -- -- NS={mtv_namespace}"))
    except Exception as ex:
        LOGGER.exception(f"Failed to run must-gather. {ex}")
