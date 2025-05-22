from typing import Any, Generator


def get_value_from_py_config(value: str, config: dict[str, Any]) -> Any:
    config_value = config.get(value)

    if not config_value:
        return config_value

    if isinstance(config_value, str):
        if config_value.lower() == "true":
            return True

        elif config_value.lower() == "false":
            return False

        else:
            return config_value

    else:
        return config_value


def get_vm_suffix(config: dict[str, Any], vms_dict: dict[str, list[str]] | None = None) -> dict[str, list[str]]:
    if not vms_dict:
        vms: dict[str, list[str]] = {
            "mtv-rhel8-sanity": [],
            "mtv-win2019-79": [],
            "mtv-rhel8-79": [],
            "mtv-rhel8-warm-394": [],
            "mtv-rhel8-warm-2disks2nics": [],
            "mtv-rhel8-warm-sanity": [],
        }

    else:
        vms = vms_dict

    for vm in vms:
        vm_suffix = ""

        if get_value_from_py_config(value="matrix_test", config=config):
            storage_name = config["storage_class"]

            if "ceph-rbd" in storage_name:
                vm_suffix = "-ceph-rbd"

            elif "nfs" in storage_name:
                vm_suffix = "-nfs"

        if get_value_from_py_config(value="release_test", config=config):
            ocp_version = config["target_ocp_version"].replace(".", "-")
            vm_suffix = f"{vm_suffix}-{ocp_version}"

        vms[vm].append(f"{vm}{vm_suffix}")

    return vms


def config_generator() -> Generator[dict[str, Any], None, None]:
    target_ocp_versions: list[str] = ["4.16", "4.17", "4.18", "4.19"]
    storages: list[str] = ["standard-csi", "ceph-rbd", "nfs-csi"]

    for ocp_version in target_ocp_versions:
        _config: dict[str, Any] = {"target_ocp_version": ocp_version, "release_test": "true", "matrix_test": "true"}

        for storage in storages:
            _config["storage_class"] = storage

            yield _config


def get_vms_names() -> dict[str, list[str]]:
    vms: dict[str, list[str]] = {}

    for config in config_generator():
        vms.update(get_vm_suffix(config=config, vms_dict=vms))

    return vms


if __name__ == "__main__":
    vms = get_vms_names()

    for base_vm, names in vms.items():
        print(f"{base_vm}:")

        for name in names:
            print(f"    {name}")

        print(f"{'*' * 80}\n")
