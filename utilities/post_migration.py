from __future__ import annotations

from typing import Any

import pytest
from ocp_resources.datavolume import DataVolume
from ocp_resources.network_map import NetworkMap
from ocp_resources.provider import Provider
from ocp_resources.storage_map import StorageMap
from pytest_testconfig import py_config
from simple_logger.logger import get_logger

from libs.base_provider import BaseProvider
from libs.forklift_inventory import ForkliftInventory
from libs.providers.rhv import OvirtProvider
from utilities.utils import rhv_provider, vmware_provider

LOGGER = get_logger(name=__name__)


def get_destination(map_resource: NetworkMap | StorageMap, source_vm_nic: dict[str, Any]) -> dict[str, Any]:
    """
    Get the source_name's (Network Or Storage) destination_name in a migration map.
    """
    for map_item in map_resource.instance.spec.map:
        result = {"name": "pod"} if map_item.destination.type == "pod" else map_item.destination

        source_vm_network = source_vm_nic["network"]

        if isinstance(source_vm_network, dict):
            source_vm_network = source_vm_network.get("name", source_vm_network.get("id", None))

        if map_item.source.type and map_item.source.type == source_vm_network:
            return result

        if (
            map_item.source.name and map_item.source.name.split("/")[1]
            if "/" in map_item.source.name
            else map_item.source.name == source_vm_network
        ):
            return result

        if map_item.source.id and map_item.source.id == source_vm_network:
            return result

    return {}


def check_cpu(source_vm: dict[str, Any], destination_vm: dict[str, Any]) -> None:
    failed_checks = {}

    src_vm_num_cores = source_vm["cpu"]["num_cores"]
    dst_vm_num_cores = destination_vm["cpu"]["num_cores"]

    src_vm_num_sockets = source_vm["cpu"]["num_sockets"]
    dst_vm_num_sockets = destination_vm["cpu"]["num_sockets"]

    if not src_vm_num_cores == dst_vm_num_cores:
        failed_checks["cpu number of cores"] = (
            f"source_vm cpu cores: {src_vm_num_cores} != destination_vm cpu cores: {dst_vm_num_cores}"
        )

    if not src_vm_num_sockets == dst_vm_num_sockets:
        failed_checks["cpu number of sockets"] = (
            f"source_vm cpu sockets: {src_vm_num_sockets} != destination_vm cpu sockets: {dst_vm_num_sockets}"
        )

    if failed_checks:
        pytest.fail(f"CPU failed checks: {failed_checks}")


def check_memory(source_vm: dict[str, Any], destination_vm: dict[str, Any]) -> None:
    assert source_vm["memory_in_mb"] == destination_vm["memory_in_mb"]


def get_nic_by_mac(nics: list[dict[str, Any]], mac_address: str) -> dict[str, Any]:
    return [nic for nic in nics if nic["macAddress"] == mac_address][0]


def check_network(source_vm: dict[str, Any], destination_vm: dict[str, Any], network_migration_map: NetworkMap) -> None:
    for source_vm_nic in source_vm["network_interfaces"]:
        expected_network = get_destination(network_migration_map, source_vm_nic)

        assert expected_network, "Network not found in migration map"

        expected_network_name = expected_network["name"]

        destination_vm_nic = get_nic_by_mac(
            nics=destination_vm["network_interfaces"], mac_address=source_vm_nic["macAddress"]
        )

        assert destination_vm_nic["network"] == expected_network_name


def check_storage(source_vm: dict[str, Any], destination_vm: dict[str, Any], storage_map_resource: StorageMap) -> None:
    destination_disks = destination_vm["disks"]
    source_vm_disks_storage = [disk["storage"]["name"] for disk in source_vm["disks"]]

    assert len(destination_disks) == len(source_vm["disks"]), "disks count"

    for destination_disk in destination_disks:
        assert destination_disk["storage"]["name"] == py_config["storage_class"], "storage class"
        if destination_disk["storage"]["name"] == "ocs-storagecluster-ceph-rbd":
            for mapping in storage_map_resource.instance.spec.map:
                if mapping.source.name in source_vm_disks_storage:
                    # The following condition is for a customer case (BZ#2064936)
                    if mapping.destination.get("accessMode"):
                        assert destination_disk["storage"]["access_mode"][0] == DataVolume.AccessMode.RWO
                    else:
                        assert destination_disk["storage"]["access_mode"][0] == DataVolume.AccessMode.RWX


def check_vms_power_state(
    source_vm: dict[str, Any], destination_vm: dict[str, Any], source_power_before_migration: bool
) -> None:
    assert source_vm["power_state"] == "off", "Checking source VM is off"

    if source_power_before_migration:
        assert destination_vm["power_state"] == source_power_before_migration


def check_guest_agent(destination_vm: dict[str, Any]) -> None:
    assert destination_vm.get("guest_agent_running"), "checking guest agent."


def check_false_vm_power_off(source_provider: OvirtProvider, source_vm: dict[str, Any]) -> None:
    """Checking that USER_STOP_VM (event.code=33) was not performed"""
    assert not source_provider.check_for_power_off_event(source_vm["provider_vm_api"]), (
        "Checking RHV VM power off was not performed (event.code=33)"
    )


def check_snapshots(
    snapshots_before_migration: list[dict[str, Any]], snapshots_after_migration: list[dict[str, Any]]
) -> None:
    failed_snapshots: list[str] = []
    snapshots_before_migration.sort(key=lambda x: x["id"])
    snapshots_after_migration.sort(key=lambda x: x["id"])

    time_format: str = "%Y-%m-%d %H:%M"

    for before_snapshot, after_snapshot in zip(snapshots_before_migration, snapshots_after_migration):
        if (
            before_snapshot["create_time"].strftime(time_format) != after_snapshot["create_time"].strftime(time_format)
            or before_snapshot["id"] != after_snapshot["id"]
            or before_snapshot["name"] != after_snapshot["name"]
            or before_snapshot["state"] != after_snapshot["state"]
        ):
            failed_snapshots.append(
                f"snapshot before migration: {before_snapshot}, snapshot after migration: {after_snapshot}"
            )

    if failed_snapshots:
        pytest.fail(f"Some of the VM snapshots did not match: {failed_snapshots}")


def check_vms(
    plan: dict[str, Any],
    source_provider: BaseProvider,
    destination_provider: BaseProvider,
    destination_namespace: str,
    network_map_resource: NetworkMap,
    storage_map_resource: StorageMap,
    source_provider_data: dict[str, Any],
    source_vms_namespace: str,
    source_provider_inventory: ForkliftInventory | None = None,
) -> None:
    res: dict[str, list[str]] = {}
    should_fail: bool = False

    for vm in plan["virtual_machines"]:
        vm_name = vm["name"]
        res[vm_name] = []

        source_vm = source_provider.vm_dict(
            name=vm_name,
            namespace=source_vms_namespace,
            source=True,
            source_provider_inventory=source_provider_inventory,
        )
        vm_guest_agent = vm.get("guest_agent")
        destination_vm = destination_provider.vm_dict(
            wait_for_guest_agent=vm_guest_agent, name=vm_name, namespace=destination_namespace
        )

        try:
            check_vms_power_state(
                source_vm=source_vm,
                destination_vm=destination_vm,
                source_power_before_migration=vm.get("source_vm_power"),
            )
        except Exception as exp:
            res[vm_name].append(str(exp))

        try:
            check_cpu(source_vm=source_vm, destination_vm=destination_vm)
        except Exception as exp:
            res[vm_name].append(str(exp))

        try:
            check_memory(source_vm=source_vm, destination_vm=destination_vm)
        except Exception as exp:
            res[vm_name].append(str(exp))

        # TODO: Remove when OCP to OCP migration is done with 2 clusters
        if source_provider.type != Provider.ProviderType.OPENSHIFT:
            try:
                check_network(
                    source_vm=source_vm,
                    destination_vm=destination_vm,
                    network_migration_map=network_map_resource,
                )
            except Exception as exp:
                res[vm_name].append(str(exp))

        try:
            check_storage(source_vm=source_vm, destination_vm=destination_vm, storage_map_resource=storage_map_resource)
        except Exception as exp:
            res[vm_name].append(str(exp))

        snapshots_before_migration = vm.get("snapshots_before_migration")

        if (
            snapshots_before_migration
            and source_provider.provider_data
            and vmware_provider(source_provider.provider_data)
        ):
            try:
                check_snapshots(
                    snapshots_before_migration=snapshots_before_migration,
                    snapshots_after_migration=source_vm["snapshots_data"],
                )
            except Exception as exp:
                res[vm_name].append(str(exp))

        if vm_guest_agent:
            try:
                check_guest_agent(destination_vm=destination_vm)
            except Exception as exp:
                res[vm_name].append(str(exp))

        if rhv_provider(source_provider_data) and isinstance(source_provider, OvirtProvider):
            try:
                check_false_vm_power_off(source_provider=source_provider, source_vm=source_vm)
            except Exception as exp:
                res[vm_name].append(str(exp))

        for _vm_name, _errors in res.items():
            if _errors:
                should_fail = True
                LOGGER.error(f"VM {_vm_name} failed checks: {_errors}")

    if should_fail:
        pytest.fail("Some of the VMs did not match")
