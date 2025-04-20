import shlex
import time

from kubernetes.dynamic import DynamicClient
from ocp_utilities.monitoring import Prometheus
from pyhelper_utils.shell import run_command
from simple_logger.logger import get_logger

LOGGER = get_logger(__name__)


def ceph_monitor_deamon(ocp_admin_client: DynamicClient) -> None:
    token_command = "oc create token prometheus-k8s -n openshift-monitoring --duration=999999s"
    _, token, _ = run_command(command=shlex.split(token_command), verify_stderr=False)
    try:
        prometheus = Prometheus(client=ocp_admin_client, verify_ssl=False, bearer_token=token)
    except Exception as exp:
        LOGGER.warning(f"Failed to get Prometheus client. {exp}")
        return

    while True:
        alerts = prometheus.get_firing_alerts(alert_name="CephOSDCriticallyFull")
        if alerts:
            LOGGER.warning("Ceph is critically full")

        time.sleep(60)
