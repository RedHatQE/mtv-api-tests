import json
import os
import re
import subprocess
import sys
from typing import Any


def usage() -> str:
    return """Usage:
run-tests-dev.sh <cluster name> --provider=<provider> --storage=<storage> [--remote] [pytest_args]
or
run-tests-dev.sh <cluster name> <pre-defined> [pytest_args]

pre-defined runs:
    vmware6-csi
    vmware6-csi-remote
    vmware7-ceph
    vmware7-ceph-remote
    vmware8-nfs
    vmware8-ceph-remote
    vmware8-csi
    openstack-ceph
    ovirt-ceph
    ova
    """


def get_cluster_version() -> str:
    res = subprocess.run(["oc", "version", "-o", "json"], capture_output=True, text=True)

    if res.returncode != 0:
        print(f"Fail to get cluster version. {res.stderr}")
        sys.exit(1)

    data = json.loads(res.stdout)
    return data["openshiftVersion"].rsplit(".", 1)[0]


def main() -> str:
    cluster_version = get_cluster_version()
    user_data_from_re = None
    user_data_from_template = None
    data = None

    runs_templates: dict[str, dict[str, Any]] = {
        "vmware6-csi": {"provider": "vmware6", "storage": "csi"},
        "vmware6-csi-remote": {"provider": "vmware6", "storage": "csi", "remote": True},
        "vmware7-ceph": {"provider": "vmware7", "storage": "ceph"},
        "vmware7-ceph-remote": {"provider": "vmware7", "storage": "ceph", "remote": True},
        "vmware8-ceph-remote": {"provider": "vmware8", "storage": "ceph", "remote": True},
        "vmware8-nfs": {"provider": "vmware8", "storage": "nfs"},
        "vmware8-csi": {"provider": "vmware8", "storage": "csi"},
        "openstack-ceph": {"provider": "openstack", "storage": "ceph"},
        "ovirt-ceph": {"provider": "ovirt", "storage": "ceph"},
        "ova": {"provider": "ova", "storage": "ceph"},
    }

    base_cmd = (
        f"uv run pytest -s --tc=matrix_test:true --tc=target_ocp_version:{cluster_version}"
        f" --tc=insecure_verify_skip:true --tc=mount_root:{os.environ['MOUNT_PATH']}"
        " --skip-data-collector"
    )

    if len(sys.argv) < 2:
        print(f"{usage()}\nPlease specify provider and storage type")
        sys.exit(1)

    template = sys.argv[1]

    if template in runs_templates:
        user_data_from_template = runs_templates[template]
        if len(sys.argv) > 2:
            user_data_from_template["others"] = " ".join(sys.argv[2:])
    else:
        user_args = " ".join(sys.argv[1:])
        user_data_from_re = re.match(
            r"(--provider=(?P<provider>\w+))? (--storage=(?P<storage>\w+))?( (?P<remote>--remote) )?((?P<others>.*))?",
            user_args,
        )

    if user_data_from_re:
        data = user_data_from_re.groupdict()

    elif user_data_from_template:
        data = user_data_from_template

    if data:
        provider = data["provider"]
        storage = data["storage"]
        remote = data.get("remote")
        pytest_args = data.get("others")

    else:
        print(f"{usage()}\nPlease specify provider and storage type")
        sys.exit(1)

    target_namespace = f"--tc=target_namespace:mtv-api-tests-{provider}-{os.environ['USER']}"

    source_provider_type = None

    if "vmware" in provider:
        source_provider_type = "--tc=source_provider_type:vsphere"

    else:
        source_provider_type = f"--tc=source_provider_type:{provider}"

    # Provider
    if provider == "vmware6":
        base_cmd += f" {source_provider_type} --tc=source_provider_version:6.5 {target_namespace}"

    elif provider == "vmware7":
        base_cmd += f" {source_provider_type} --tc=source_provider_version:7.0.3 {target_namespace}"

    elif provider == "vmware8":
        base_cmd += f" {source_provider_type} --tc=source_provider_version:8.0.1 {target_namespace}"

    elif provider == "ovirt":
        base_cmd += f" {source_provider_type} --tc=source_provider_version:4.4.9 {target_namespace}"

    elif provider == "openstack":
        base_cmd += f" {source_provider_type} --tc=source_provider_version:psi {target_namespace}"

    elif provider == "ova":
        base_cmd += f" {source_provider_type} --tc=source_provider_version:nfs {target_namespace}"

    # Remote
    if remote:
        base_cmd += f" -m remote --tc=remote_ocp_cluster:{os.environ['CLUSTER_NAME']}"

    elif provider == "ova":
        base_cmd += " -m ova"

    else:
        base_cmd += " -m tier0"

    # Storage
    if storage == "ceph":
        base_cmd += " --tc=storage_class:ocs-storagecluster-ceph-rbd"

    elif storage == "nfs":
        base_cmd += " --tc=storage_class:nfs-csi"

    elif storage == "csi":
        base_cmd += " --tc=storage_class:standard-csi"

    if pytest_args:
        base_cmd += f" {pytest_args}"

    return base_cmd


if __name__ == "__main__":
    print(main())
