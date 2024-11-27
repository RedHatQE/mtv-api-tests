from pytest_testconfig import config
from utilities.utils import get_guest_os_credentials, rhv_provider, vmware_provider
from subprocess import STDOUT, check_output
import uuid
import pytest_check as check

RWO = "ReadWriteOnce"
RWX = "ReadWriteMany"


def get_destination(map_resource, source_vm_nic):
    """
    Get the source_name's (Network Or Storage) destination_name in a migration map.
    """
    for map_item in map_resource.instance.spec.map:
        result = {"name": "pod"} if map_item.destination.type == "pod" else map_item.destination
        if map_item.source.type:
            if map_item.source.type == source_vm_nic["network"]:
                return result
            if map_item.source.name and map_item.source.name.split("/")[1] == source_vm_nic["network"]:
                return result
        else:
            if map_item.source.id and map_item.source.id == source_vm_nic["network"].get("id", None):
                return result
            if map_item.source.name and map_item.source.name == source_vm_nic["network"].get("name", None):
                return result

    return None


def check_cpu(source_vm, destination_vm):
    check.equal(source_vm["cpu"]["num_cores"], destination_vm["cpu"]["num_cores"])
    check.equal(source_vm["cpu"]["num_sockets"], destination_vm["cpu"]["num_sockets"])


def check_memory(source_vm, destination_vm):
    check.equal(source_vm["memory_in_mb"], destination_vm["memory_in_mb"])


def get_nic_by_mac(nics, mac_address):
    return [nic for nic in nics if nic["macAddress"] == mac_address][0]


def check_network(source_provider_data, source_vm, destination_vm, network_migration_map):
    for source_vm_nic in source_vm["network_interfaces"]:
        # for rhv we use networks ids instead of names
        # TODO: Use datacenter/name format for rhv
        expected_network_name = get_destination(network_migration_map, source_vm_nic)["name"]
        destination_vm_nic = get_nic_by_mac(
            nics=destination_vm["network_interfaces"], mac_address=source_vm_nic["macAddress"]
        )

        check.equal(destination_vm_nic["network"], expected_network_name)


def check_storage(source_vm, destination_vm, storage_map_resource):
    destination_disks = destination_vm["disks"]
    source_vm_disks_storage = [disk["storage"]["name"] for disk in source_vm["disks"]]
    check.equal(len(destination_disks), len(source_vm["disks"]), "disks count")
    for destination_disk in destination_disks:
        check.equal(destination_disk["storage"]["name"], config["storage_class"], "storage class")
        if destination_disk["storage"]["name"] == "ocs-storagecluster-ceph-rbd":
            for mapping in storage_map_resource.instance.spec.map:
                if mapping.source.name in source_vm_disks_storage:
                    # The following condition is for a customer case (BZ#2064936)
                    if mapping.destination.get("accessMode"):
                        check.equal(destination_disk["storage"]["access_mode"][0], RWO)
                    else:
                        check.equal(destination_disk["storage"]["access_mode"][0], RWX)


def check_migration_network(source_provider_data, destination_vm):
    for disk in destination_vm["disks"]:
        check.is_in(source_provider_data["host_list"][0]["migration_host_ip"], disk["vddk_url"])


def check_source_vm_snapshots(vm_snapshots_before, vm_snapshots_after):
    check.equal(vm_snapshots_before, vm_snapshots_after, "Checking source VM snapshots")


def check_data_integrity(source_vm_dict, destination_vm_dict, source_provider_data, min_number_of_snapshots):
    """
    Reads the content of the data file that was generated during the test on the source vm
    And Verify the integrity of the  data generated after each snapshot
    Note: Only works when MTV and the Target Provider are deployed on the same cluster
    """
    ip_address = destination_vm_dict["network_interfaces"][0]["ip"]
    os_user, os_password = get_guest_os_credentials(provider_data=source_provider_data, vm_dict=source_vm_dict)

    pod_name = f"worker-{str(uuid.uuid4())[:5]}"
    cli = f'"python" "./main.py"  "--ip={ip_address}"   "--username={os_user}" "--password={os_password}"'
    data = check_output(
        [
            "/bin/sh",
            "-c",
            f"oc project {config['target_namespace']} && oc run {pod_name} --image=quay.io/mtvqe/python-runner \
             --command -- {cli}  && sleep 10 && oc logs pod/{pod_name} && oc delete pod/{pod_name}&>/dev/null &",
        ],
        stderr=STDOUT,
    )

    # we expect: -1|1|2|3|.|n|.|.| n>= the underlined minimum number of snapshots
    print(data)
    data = data.decode("utf8").split("-1")[1].split("|")
    for i in range(1, len(data)):
        check.equal(data[i], str(i), "data integrity check.")
    check.greater_equal(len(data) - 1, min_number_of_snapshots, "data integrity check.")


def check_vms_power_state(source_vm, destination_vm, source_power_before_migration):
    check.equal(source_vm["power_state"], "off", "Checking source VM is off")
    if source_power_before_migration:
        check.equal(destination_vm["power_state"], source_power_before_migration)


def check_guest_agent(destination_vm):
    check.is_true(destination_vm.get("guest_agent_running"), "checking guest agent.")


def check_false_vm_power_off(source_provider, source_vm):
    """Checking that USER_STOP_VM (event.code=33) was not performed"""
    check.is_false(
        source_provider.check_for_power_off_event(source_vm["provider_vm_api"]),
        "Checking RHV VM power off was not performed (event.code=33)",
    )


def check_vms(
    plan,
    source_provider,
    destination_provider,
    destination_namespace,
    network_map_resource,
    storage_map_resource,
    source_provider_host,
    source_provider_data,
):
    virtual_machines = plan["virtual_machines"]
    for vm in virtual_machines:
        source_vm = source_provider.vm_dict(name=vm["name"], namespace=config["target_namespace"], source=True)

        # Skip checking guest agent for rhv vm with multiple disks
        # because of bug https://issues.redhat.com/browse/MTV-433
        vm_guest_agent = vm.get("guest_agent")
        # if rhv_provider(source_provider_data) and "disks" in vm["name"]:
        #     vm_guest_agent = False

        destination_vm = destination_provider.vm_dict(
            wait_for_guest_agent=vm_guest_agent, name=vm["name"], namespace=destination_namespace
        )

        check_vms_power_state(
            source_vm=source_vm, destination_vm=destination_vm, source_power_before_migration=vm.get("source_vm_power")
        )

        check_cpu(source_vm=source_vm, destination_vm=destination_vm)
        check_memory(source_vm=source_vm, destination_vm=destination_vm)
        check_network(
            source_provider_data=source_provider_data,
            source_vm=source_vm,
            destination_vm=destination_vm,
            network_migration_map=network_map_resource,
        )
        check_storage(source_vm=source_vm, destination_vm=destination_vm, storage_map_resource=storage_map_resource)
        if source_provider_host and source_provider_data:
            check_migration_network(source_provider_data=source_provider_data, destination_vm=destination_vm)

        if plan.get("warm_migration") and plan.get("pre_copies_before_cut_over"):
            check_data_integrity(
                destination_vm_dict=destination_vm,
                source_vm_dict=source_vm,
                source_provider_data=source_provider_data,
                min_number_of_snapshots=plan["pre_copies_before_cut_over"],
            )

        # TODO: Remove the condition "if cold" once these two bugs are fixed:
        #  https://bugzilla.redhat.com/show_bug.cgi?id=2053183
        #  https://github.com/kubev2v/forklift/issues/301
        if "snapshots_before_migration" in vm and vmware_provider(source_provider.provider_data):
            check_source_vm_snapshots(
                vm_snapshots_before=vm["snapshots_before_migration"], vm_snapshots_after=source_vm["snapshots_data"]
            )

        if vm_guest_agent:
            check_guest_agent(destination_vm=destination_vm)

        if rhv_provider(source_provider_data):
            check_false_vm_power_off(source_provider=source_provider, source_vm=source_vm)
