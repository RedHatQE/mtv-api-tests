import contextlib
import shlex
import time

from kubernetes.dynamic import DynamicClient
from ocp_resources.exceptions import ExecOnPodError
from ocp_resources.pod import Pod
from ocp_utilities.monitoring import Prometheus
from pyhelper_utils.shell import run_command
from simple_logger.logger import get_logger

LOGGER = get_logger(__name__)


def ceph_cleanup_deamon(ocp_admin_client: DynamicClient, ceph_tools_pod: Pod) -> None:
    token_command = "oc create token prometheus-k8s -n openshift-monitoring --duration=999999s"
    _, token, _ = run_command(command=shlex.split(token_command), verify_stderr=False)
    prometheus = Prometheus(client=ocp_admin_client, verify_ssl=False, bearer_token=token)
    while True:
        alerts = prometheus.get_firing_alerts(alert_name="CephOSDCriticallyFull")
        if alerts:
            run_ceph_cleanup(ceph_tools_pod=ceph_tools_pod)

        time.sleep(60)


def run_ceph_cleanup(ceph_tools_pod: Pod) -> None:
    with contextlib.suppress(ExecOnPodError):
        snaps: list[str] = []
        vols: list[str] = []
        ceph_pool_name = "ocs-storagecluster-cephblockpool"
        set_full_ratio_cmd = "ceph osd set-full-ratio"

        try:
            LOGGER.warning("Cleaning up ceph storage")
            ceph_tools_pod.execute(command=shlex.split(f"{set_full_ratio_cmd} 0.90"), ignore_rc=True)

            for line in ceph_tools_pod.execute(
                command=shlex.split(f"rbd ls {ceph_pool_name}"), ignore_rc=True
            ).splitlines():
                if "snap" in line:
                    snaps.append(line)

                elif "vol" in line:
                    vols.append(line)

            for _snap in snaps:
                ceph_tools_pod.execute(command=shlex.split(f"rbd snap purge {ceph_pool_name}/{_snap}"), ignore_rc=True)

            for _vol in vols:
                ceph_tools_pod.execute(command=shlex.split(f"rbd rm {ceph_pool_name}/{_vol}"), ignore_rc=True)

            for _trash in ceph_tools_pod.execute(
                command=shlex.split(f"rbd trash list {ceph_pool_name}"), ignore_rc=True
            ).splitlines():
                _trash_name = _trash.split()[0]
                ceph_tools_pod.execute(
                    command=shlex.split(f"rbd trash remove {ceph_pool_name}/{_trash_name}"), ignore_rc=True
                )
        except ExecOnPodError:
            LOGGER.error("Failed to run ceph cleanup")

        finally:
            ceph_tools_pod.execute(command=shlex.split(f"{set_full_ratio_cmd} 0.85"), ignore_rc=True)

    # sleep_time = 60 * 5
    #
    # try:
    #     while True:
    #         try:
    #             snaps: list[str] = []
    #             vols: list[str] = []
    #             ceph_pool_name = "ocs-storagecluster-cephblockpool"
    #             if get_ceph_pool_percent_used(ceph_tools_pod, ceph_pool_name) < 80:
    #                 time.sleep(sleep_time)
    #                 continue
    #
    #             LOGGER.warning("Cleaning up ceph storage")
    #             ceph_tools_pod.execute(command=shlex.split("ceph osd set-full-ratio 0.90"), ignore_rc=True)
    #
    #             for line in ceph_tools_pod.execute(
    #                 command=shlex.split(f"rbd ls {ceph_pool_name}"), ignore_rc=True
    #             ).splitlines():
    #                 if "snap" in line:
    #                     snaps.append(line)
    #
    #                 elif "vol" in line:
    #                     vols.append(line)
    #
    #             for _snap in snaps:
    #                 ceph_tools_pod.execute(
    #                     command=shlex.split(f"rbd snap purge {ceph_pool_name}/{_snap}"), ignore_rc=True
    #                 )
    #
    #             for _vol in vols:
    #                 ceph_tools_pod.execute(command=shlex.split(f"rbd rm {ceph_pool_name}/{_vol}"), ignore_rc=True)
    #
    #             for _trash in ceph_tools_pod.execute(
    #                 command=shlex.split(f"rbd trash list {ceph_pool_name}"), ignore_rc=True
    #             ).splitlines():
    #                 _trash_name = _trash.split()[0]
    #                 ceph_tools_pod.execute(
    #                     command=shlex.split(f"rbd trash remove {ceph_pool_name}/{_trash_name}"), ignore_rc=True
    #                 )
    #
    #             time.sleep(sleep_time)
    #         except ExecOnPodError:
    #             continue
    #
    # finally:
    #     ceph_tools_pod.execute(command=shlex.split("ceph osd set-full-ratio 0.85"), ignore_rc=True)
    #


# def get_ceph_pool_percent_used(ceph_tools_pod: Pod, pool_name: str) -> int | float:
#     _df = json.loads(ceph_tools_pod.execute(command=shlex.split("ceph df -f json"), ignore_rc=True))
#     for pool in _df["pools"]:
#         if pool["name"] == pool_name:
#             percent_used: int | float = pool["stats"]["percent_used"]
#             return percent_used
#
#     return 0
