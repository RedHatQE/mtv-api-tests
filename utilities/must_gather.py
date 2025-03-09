import shlex
from pathlib import Path

from ocp_resources.cluster_service_version import ClusterServiceVersion
from ocp_resources.resource import get_client
from ocp_resources.subscription import Subscription
from pyhelper_utils.shell import run_command
from pytest_testconfig import py_config
from simple_logger.logger import get_logger

LOGGER = get_logger(__name__)


def run_must_gather(data_collector_path: Path, plans: list[str]) -> None:
    ocp_admin_client = get_client()
    mtv_subs = Subscription(client=ocp_admin_client, name="mtv-operator", namespace=py_config["mtv_namespace"])
    if not mtv_subs.exists:
        LOGGER.error("Can't find MTV Subscription")
        return

    installed_csv = mtv_subs.instance.status.installedCSV
    mtv_csv = ClusterServiceVersion(client=ocp_admin_client, name=installed_csv, namespace=py_config["mtv_namespace"])
    if not mtv_csv.exists:
        LOGGER.error(f"Can't find MTV ClusterServiceVersion for {installed_csv}")
        return

    must_gather_images = [
        image["image"] for image in mtv_csv.instance.spec.relatedImages if "must-gather" in image["name"]
    ]
    if not must_gather_images:
        LOGGER.error("Can't find any must-gather image under MTV ClusterServiceVersion")
        return

    _must_gather_base_cmd = f"oc adm must-gather --image={must_gather_images[0]} --dest-dir={data_collector_path}"

    try:
        if plans:
            for plan_name in plans:
                run_command(shlex.split(f"{_must_gather_base_cmd} -- PLAN={plan_name} /usr/bin/targeted"))
        else:
            run_command(shlex.split(f"{_must_gather_base_cmd}"))
    except Exception:
        LOGGER.error("Failed to run musg-gather")
