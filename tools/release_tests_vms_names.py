from typing import Any, Generator

from pyVim.connect import SmartConnect
from pyVmomi import vim


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


def get_missing_vms_in_vmware(vms: dict[str, list[str]]) -> dict[str, list[str]]:
    vmware_servers_config = {
        "vsphere-6.5": {
            "fqdn": "rhev-node-05.rdu2.scalelab.redhat.com",
            "username": "mtv@vsphere.local",
            "password": "Heslo123!",
        },
        "vsphere-7.0.3": {
            "fqdn": "10.6.46.170",
            "username": "administrator@vsphere.local",
            "password": "VCENTER@redhat2023",
        },
        "vsphere-8.0.1": {
            "fqdn": "10.6.46.249",
            "username": "administrator@vsphere.local",
            "password": "VCENTER@redhat2023",
        },
    }

    missing_vms: dict[str, list[str]] = {}
    required_vms: list[str] = []
    for list_vms in vms.values():
        required_vms.extend(list_vms)

    for server, data in vmware_servers_config.items():
        api = SmartConnect(  # ssl cert check is not required
            host=data["fqdn"],
            user=data["username"],
            pwd=data["password"],
            port=443,
            disableSslCertValidation=True,
        )
        content = api.RetrieveContent()
        view_manager = content.viewManager
        datacenters = content.rootFolder.childEntity
        container_view = view_manager.CreateContainerView(
            container=datacenters[0].vmFolder, type=[vim.VirtualMachine], recursive=True
        )
        server_vms = [vm.name for vm in container_view.view]
        for vm in required_vms:
            if vm not in server_vms:
                missing_vms.setdefault(server, []).append(vm)

    return missing_vms


if __name__ == "__main__":
    vms = get_vms_names()
    missing_vms = get_missing_vms_in_vmware(vms=vms)

    # for base_vm, names in vms.items():
    #     print(f"{base_vm}:")
    #
    #     for name in names:
    #         print(f"    {name}")
    #
    #     print(f"{'*' * 80}\n")

    for vmware, names in missing_vms.items():
        print(f"{vmware}:")

        for name in names:
            print(f"    {name}")

        print(f"{'*' * 80}\n")
