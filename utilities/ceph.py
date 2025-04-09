"""
POD_EXEC_CMD="oc exec -n openshift-storage $TOOLS_POD"
  CEPH_POOL="ocs-storagecluster-cephblockpool"
  echo "$POD_EXEC_CMD" -- ceph osd set-full-ratio 0.90

  RBD_LIST=$($POD_EXEC_CMD -- rbd ls "$CEPH_POOL")
  for SNAP_AND_VOL in $RBD_LIST; do
    SNAP_AND_VOL_PATH="$CEPH_POOL/$SNAP_AND_VOL"
    if grep -q "snap" <<<"$SNAP_AND_VOL"; then
      echo "$POD_EXEC_CMD" -- rbd snap purge "$SNAP_AND_VOL_PATH"
    fi
    if grep -q "vol" <<<"$SNAP_AND_VOL"; then
      echo "$POD_EXEC_CMD" -- rbd rm "$SNAP_AND_VOL_PATH"
    fi
  done

  RBD_TRASH_LIST=$($POD_EXEC_CMD -- rbd trash list "$CEPH_POOL" | awk -F" " '{print$1}')
  for TRASH in $RBD_TRASH_LIST; do
    TRASH_ITEM_PATH="$CEPH_POOL/$TRASH"
    echo "$POD_EXEC_CMD" -- rbd trash remove "$TRASH_ITEM_PATH"
  done

  echo "$POD_EXEC_CMD" -- ceph osd set-full-ratio 0.85
  echo "$POD_EXEC_CMD" -- ceph df
"""

import shlex

from ocp_resources.pod import Pod


def ceph_cleanup(ceph_tools_pod: Pod) -> None:
    try:
        snaps: list[str] = []
        vols: list[str] = []
        ceph_pool_name = "ocs-storagecluster-cephblockpool"
        ceph_tools_pod.execute(command=shlex.split("ceph osd set-full-ratio 0.90"))
        for line in ceph_tools_pod.execute(command=shlex.split(f"rbd ls {ceph_pool_name}")).splitlines():
            if "snap" in line:
                snaps.append(line)

            elif "vol" in line:
                vols.append(line)

        for _snap in snaps:
            ceph_tools_pod.execute(command=shlex.split(f"rbd snap purge {ceph_pool_name}/{_snap}"))

        for _vol in vols:
            ceph_tools_pod.execute(command=shlex.split(f"rbd rm {ceph_pool_name}/{_vol}"))

        for _trash in ceph_tools_pod.execute(command=shlex.split(f"rbd trash list {ceph_pool_name}")).splitlines():
            _trash_name = _trash.split()[0]
            ceph_tools_pod.execute(command=shlex.split(f"rbd trash remove {ceph_pool_name}/{_trash_name}"))
    finally:
        ceph_tools_pod.execute(command=shlex.split("ceph osd set-full-ratio 0.85"))
