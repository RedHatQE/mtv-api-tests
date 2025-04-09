import shlex
import time

from ocp_resources.exceptions import ExecOnPodError
from ocp_resources.pod import Pod
from simple_logger.logger import get_logger

LOGGER = get_logger(__name__)


def run_ceph_cleanup(ceph_tools_pod: Pod) -> None:
    LOGGER.info("Running ceph cleanup")

    try:
        while True:
            try:
                snaps: list[str] = []
                vols: list[str] = []
                ceph_pool_name = "ocs-storagecluster-cephblockpool"
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

                time.sleep(60 * 5)
            except ExecOnPodError:
                continue

    finally:
        ceph_tools_pod.execute(command=shlex.split("ceph osd set-full-ratio 0.85"), ignore_rc=True)
