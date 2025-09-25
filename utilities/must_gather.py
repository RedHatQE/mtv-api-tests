import shlex
from pathlib import Path

from ocp_resources.cluster_service_version import ClusterServiceVersion
from ocp_resources.subscription import Subscription
from pyhelper_utils.shell import run_command
from pytest_testconfig import py_config
from simple_logger.logger import get_logger

from utilities.utils import get_cluster_client

LOGGER = get_logger(__name__)


def run_must_gather(data_collector_path: Path, plan: dict[str, str] | None = None) -> None:
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
        must_gather_images = [env["value"] for env in mtv_envs if env["name"] == "MUST_GATHER_IMAGE"]

        if not must_gather_images:
            LOGGER.error("Can't find any must-gather image under MTV ClusterServiceVersion using upsream image")
            must_gather_images = ["quay.io/kubev2v/forklift-must-gather:latest"]
            return

        _must_gather_base_cmd = f"oc adm must-gather --image={must_gather_images[0]} --dest-dir={data_collector_path}"

        if plan:
            plan_name = plan["name"]
            plan_namespace = plan["namespace"]
            run_command(
                shlex.split(f"{_must_gather_base_cmd} -- NS={plan_namespace} PLAN={plan_name} /usr/bin/targeted")
            )
        else:
            run_command(shlex.split(f"{_must_gather_base_cmd} -- -- NS={mtv_namespace}"))
    except Exception as ex:
        LOGGER.error(f"Failed to run musg-gather. {ex}")
