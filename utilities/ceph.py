import shlex
import time

from kubernetes.dynamic import DynamicClient
from ocp_resources.pod import Pod
from ocp_utilities.monitoring import Prometheus
from pyhelper_utils.shell import run_command
from simple_logger.logger import get_logger

LOGGER = get_logger(__name__)


def ceph_monitor_deamon(ocp_admin_client: DynamicClient, ceph_tools_pod: Pod) -> None:
    token_command = "oc create token prometheus-k8s -n openshift-monitoring --duration=999999s"
    _, token, _ = run_command(command=shlex.split(token_command), verify_stderr=False)
    prometheus = Prometheus(client=ocp_admin_client, verify_ssl=False, bearer_token=token)
    while True:
        alerts = prometheus.get_firing_alerts(alert_name="CephOSDCriticallyFull")
        if alerts:
            LOGGER.warning("Ceph is critically full")

        time.sleep(60)
