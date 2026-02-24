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
    and prepends ``devel-testing-for-``.

    Args:
        channel (str): The Subscription channel string (e.g. ``release-v2.11``).

    Returns:
        str: The derived IDMS resource name.

    Raises:
        ValueError: If ``channel`` is empty.
    """
    if not channel:
        raise ValueError("Subscription channel is empty")

    stripped = channel.removeprefix("release-v")
    return f"devel-testing-for-{stripped.replace('.', '-')}"


def _get_must_gather_mirror_url(idms: ImageDigestMirrorSet) -> str:
    """Extract the must-gather mirror URL from an ImageDigestMirrorSet.

    Iterates over ``imageDigestMirrors`` entries and returns the first mirror
    URL from the entry whose ``source`` contains ``must-gather``.

    Args:
        idms (ImageDigestMirrorSet): The IDMS resource to inspect.

    Returns:
        str: The first mirror URL for the must-gather image.

    Raises:
        ValueError: If no ``imageDigestMirrors`` entry contains ``must-gather``
            in its source, or if the matching entry has an empty mirrors list.
    """
    for mirror_entry in idms.instance.spec.imageDigestMirrors:
        if "must-gather" in mirror_entry["source"]:
            mirrors = mirror_entry.get("mirrors", [])
            if not mirrors:
                raise ValueError(f"IDMS '{idms.name}' has must-gather entry with no mirrors")
            return mirrors[0]

    raise ValueError(f"No must-gather entry found in IDMS '{idms.name}'")


def _resolve_must_gather_image(
    ocp_admin_client: DynamicClient,
    mtv_subs: Subscription,
    csv_image: str,
) -> str:
    """Resolve the must-gather image via IDMS.

    Builds the IDMS resource name from the Subscription channel, retrieves the
    mirror URL, extracts the SHA from the CSV image, and combines them.

    Args:
        ocp_admin_client (DynamicClient): The OpenShift admin client.
        mtv_subs (Subscription): The MTV operator Subscription resource.
        csv_image (str): The must-gather image reference from the CSV
            (used for SHA extraction).

    Returns:
        str: The resolved must-gather image string.

    Raises:
        ValueError: If the Subscription channel is empty, no must-gather entry
            is found in the IDMS, or the CSV image has no digest separator.
        NotFoundError: If the IDMS resource does not exist on the cluster.
    """
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

        mtv_envs = mtv_csv.instance.spec.install.spec.deployments[0].spec.template.spec.containers[0].env
        csv_must_gather_images = [env["value"] for env in mtv_envs if env["name"] == "MUST_GATHER_IMAGE"]

        if not csv_must_gather_images:
            raise ValueError(f"No MUST_GATHER_IMAGE found in MTV ClusterServiceVersion '{installed_csv}'")

        must_gather_image = _resolve_must_gather_image(
            ocp_admin_client=ocp_admin_client,
            mtv_subs=mtv_subs,
            csv_image=csv_must_gather_images[0],
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
        LOGGER.error(f"Failed to run must-gather. {ex}")
