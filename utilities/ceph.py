import json
import shlex
import time

from ocp_resources.exceptions import ExecOnPodError
from ocp_resources.pod import Pod
from simple_logger.logger import get_logger

LOGGER = get_logger(__name__)


def run_ceph_cleanup(ceph_tools_pod: Pod) -> None:
    LOGGER.info("Running ceph cleanup")
    sleep_time = 60 * 5

    try:
        while True:
            try:
                snaps: list[str] = []
                vols: list[str] = []
                ceph_pool_name = "ocs-storagecluster-cephblockpool"
                if get_ceph_pool_percent_used(ceph_tools_pod, ceph_pool_name) < 60:
                    time.sleep(sleep_time)
                    continue

                ceph_tools_pod.execute(command=shlex.split("ceph osd set-full-ratio 0.90"), ignore_rc=True)

                for line in ceph_tools_pod.execute(
                    command=shlex.split(f"rbd ls {ceph_pool_name}"), ignore_rc=True
                ).splitlines():
                    if "snap" in line:
                        snaps.append(line)

                    elif "vol" in line:
                        vols.append(line)

                for _snap in snaps:
                    ceph_tools_pod.execute(
                        command=shlex.split(f"rbd snap purge {ceph_pool_name}/{_snap}"), ignore_rc=True
                    )

                for _vol in vols:
                    ceph_tools_pod.execute(command=shlex.split(f"rbd rm {ceph_pool_name}/{_vol}"), ignore_rc=True)

                for _trash in ceph_tools_pod.execute(
                    command=shlex.split(f"rbd trash list {ceph_pool_name}"), ignore_rc=True
                ).splitlines():
                    _trash_name = _trash.split()[0]
                    ceph_tools_pod.execute(
                        command=shlex.split(f"rbd trash remove {ceph_pool_name}/{_trash_name}"), ignore_rc=True
                    )

                time.sleep(sleep_time)
            except ExecOnPodError:
                continue

    finally:
        ceph_tools_pod.execute(command=shlex.split("ceph osd set-full-ratio 0.85"), ignore_rc=True)


def get_ceph_pool_percent_used(ceph_tools_pod: Pod, pool_name: str) -> int | float:
    _df = json.loads(ceph_tools_pod.execute(command=shlex.split("ceph df -f json"), ignore_rc=True))
    for pool in _df["pools"]:
        if pool["name"] == pool_name:
            percent_used: int | float = pool["stats"]["percent_used"]
            return percent_used

    return 0
