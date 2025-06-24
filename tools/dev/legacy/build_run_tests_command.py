import argparse
import os
import sys
from typing import Any, NamedTuple

from packaging.version import Version


class ProviderConfig(NamedTuple):
    type: str
    version: str


# Provider and storage configurations
PROVIDER_MAP: dict[str, ProviderConfig] = {
    "vmware6": ProviderConfig("vsphere", "6.5"),
    "vmware7": ProviderConfig("vsphere", "7.0.3"),
    "vmware8": ProviderConfig("vsphere", "8.0.1"),
    "ovirt": ProviderConfig("ovirt", "4.4.9"),
    "openstack": ProviderConfig("openstack", "psi"),
    "ova": ProviderConfig("ova", "nfs"),
}

STORAGE_MAP: dict[str, str] = {
    "ceph": "ocs-storagecluster-ceph-rbd",
    "nfs": "nfs-csi",
    "csi": "standard-csi",
}

RUNS_TEMPLATES: dict[str, dict[str, Any]] = {
    "vmware6-csi": {"provider": "vmware6", "storage": "csi"},
    "vmware6-csi-remote": {"provider": "vmware6", "storage": "csi", "remote": True},
    "vmware7-ceph": {"provider": "vmware7", "storage": "ceph"},
    "vmware7-ceph-remote": {"provider": "vmware7", "storage": "ceph", "remote": True},
    "vmware8-ceph-remote": {"provider": "vmware8", "storage": "ceph", "remote": True},
    "vmware8-nfs": {"provider": "vmware8", "storage": "nfs"},
    "vmware8-csi": {"provider": "vmware8", "storage": "csi"},
    "openstack-ceph": {"provider": "openstack", "storage": "ceph"},
    "openstack-csi": {"provider": "openstack", "storage": "csi"},
    "ovirt-ceph": {"provider": "ovirt", "storage": "ceph"},
    "ovirt-csi": {"provider": "ovirt", "storage": "csi"},
    "ovirt-csi-remote": {"provider": "ovirt", "storage": "csi", "remote": True},
    "ova-ceph": {"provider": "ova", "storage": "ceph"},
}


def usage() -> str:
    return """Usage:
run-tests-dev.sh <cluster name> --provider=<provider> --storage=<storage> [--remote] [pytest_args] [--data-collect]
or
run-tests-dev.sh <cluster name> <pre-defined> [pytest_args] [--data-collect]

pre-defined runs:
    vmware6-csi
    vmware6-csi-remote
    vmware7-ceph
    vmware7-ceph-remote
    vmware8-nfs
    vmware8-ceph-remote
    vmware8-csi
    openstack-ceph
    openstack-csi
    ovirt-ceph
    ovirt-csi
    ovirt-csi-remote
    ova
    """


def parse_args() -> argparse.Namespace:
    """Parses command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Build pytest command for MTV API tests.",
        formatter_class=argparse.RawTextHelpFormatter,
    )

    # Positional argument for pre-defined templates or passthrough for manual flags
    parser.add_argument(
        "template",
        nargs="?",
        help="A pre-defined run template (e.g., vmware7-ceph) or provider/storage flags.",
    )

    # Manual configuration flags
    parser.add_argument("--provider", help="Source provider type (e.g., vmware8, ovirt).")
    parser.add_argument("--storage", help="Storage class type (e.g., ceph, nfs, csi).")
    parser.add_argument("--remote", action="store_true", help="Flag for remote cluster tests.")
    parser.add_argument("--data-collect", action="store_true", help="Enable data collector for failed tests.")
    parser.add_argument("--release-test", action="store_true", help="Flag for release-specific tests.")
    parser.add_argument("--cluster-version", required=True, help="OpenShift cluster version (e.g., 4.15).")

    args, unknown_args = parser.parse_known_args()
    args.pytest_args = unknown_args
    return args


def build_pytest_command(args: argparse.Namespace) -> str:
    """Builds the final pytest command string."""
    version = Version(args.cluster_version)
    cluster_version = f"{version.major}.{version.minor}"
    base_cmd_parts: list[str] = [
        "uv run pytest -s",
        f"--tc=target_ocp_version:{cluster_version}",
        "--tc=insecure_verify_skip:true",
        f"--tc=mount_root:{os.environ['MOUNT_PATH']}",
    ]

    # Handle pre-defined templates
    if args.template and args.template in RUNS_TEMPLATES:
        template_data = RUNS_TEMPLATES[args.template]
        provider_key = template_data["provider"]
        storage_key = template_data["storage"]
        is_remote = template_data.get("remote", False)
    elif args.provider and args.storage:
        provider_key = args.provider
        storage_key = args.storage
        is_remote = args.remote
    else:
        print("Error: You must specify a pre-defined template or both --provider and --storage.", file=sys.stderr)
        sys.exit(1)

    # Get provider and storage configurations
    provider_config = PROVIDER_MAP.get(provider_key)
    storage_class = STORAGE_MAP.get(storage_key)

    if not provider_config or not storage_class:
        print(f"Error: Invalid provider '{provider_key}' or storage '{storage_key}'.", file=sys.stderr)
        sys.exit(1)

    # Append provider and storage test configurations
    base_cmd_parts.extend([
        f"--tc=source_provider_type:{provider_config.type}",
        f"--tc=source_provider_version:{provider_config.version}",
        f"--tc=target_namespace:mtv-api-tests-{provider_key}-{os.environ['USER']}",
        f"--tc=storage_class:{storage_class}",
    ])

    if is_remote:
        base_cmd_parts.append(f"-m remote --tc=remote_ocp_cluster:{os.environ['CLUSTER_NAME']}")

    # Handle test types and data collection
    if not args.data_collect:
        base_cmd_parts.append("--skip-data-collector")

    if not args.release_test:
        base_cmd_parts.append("--tc=matrix_test:true -m tier0")

    # Add any extra pytest arguments
    if args.pytest_args:
        base_cmd_parts.append(" ".join(args.pytest_args))

    return " ".join(base_cmd_parts)


def main() -> None:
    """Main function."""
    args = parse_args()
    pytest_command = build_pytest_command(args)
    print(pytest_command)


if __name__ == "__main__":
    main()
