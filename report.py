from subprocess import check_output, STDOUT
from pytest_testconfig import config
from statistics import mean
import os
import datetime as dt
import uuid


class MigrateData(object):
    def __init__(
        self,
        vm_name,
        total_disks=None,
        total_disks_size=None,
        disk_size_unit=None,
        initialize=None,
        disk_transfer=None,
        cutover=None,
        image_conversion=None,
        precopy=None,
        vm_migrate=None,
    ):
        self.vm_name = vm_name
        self.total_disks = total_disks
        self.total_disks_size = total_disks_size
        self.disk_size_unit = disk_size_unit
        self.precopy = precopy
        self.initialize = initialize
        self.disk_transfer = disk_transfer
        self.cutover = cutover
        self.image_conversion = image_conversion
        self.vm_migrate = vm_migrate


def get_migration_data_from_plan(plan_resource):
    list_vms = []

    # find MigrationType: COLD/WARM
    migration_type = "WARM" if plan_resource.instance.spec.warm else "COLD"

    # Collect data from resource_plan
    for vm in plan_resource.instance.status.migration.vms:
        current_vm = MigrateData(vm_name=vm.name)
        current_vm.vm_migrate = dt.datetime.strptime(vm.completed, "%Y-%m-%dT%H:%M:%SZ") - dt.datetime.strptime(
            vm.started, "%Y-%m-%dT%H:%M:%SZ"
        )
        for actions in vm.pipeline:
            if "Initialize" in actions.name:
                current_vm.initialize = dt.datetime.strptime(
                    actions.completed, "%Y-%m-%dT%H:%M:%SZ"
                ) - dt.datetime.strptime(actions.started, "%Y-%m-%dT%H:%M:%SZ")
            elif "DiskTransfer" in actions.name:
                current_vm.disk_transfer = dt.datetime.strptime(
                    actions.completed, "%Y-%m-%dT%H:%M:%SZ"
                ) - dt.datetime.strptime(actions.started, "%Y-%m-%dT%H:%M:%SZ")
                current_vm.total_disks_size = actions.progress.total
                current_vm.disk_size_unit = actions.annotations.unit
                current_vm.total_disks = len(actions.tasks)
                if migration_type == "WARM":
                    current_vm.precopy = actions.tasks[0].annotations.Precopy
            elif "ImageConversion" in actions.name:
                current_vm.image_conversion = dt.datetime.strptime(
                    actions.completed, "%Y-%m-%dT%H:%M:%SZ"
                ) - dt.datetime.strptime(actions.started, "%Y-%m-%dT%H:%M:%SZ")
            elif "Cutover" in actions.name:
                current_vm.cutover = dt.datetime.strptime(
                    actions.completed, "%Y-%m-%dT%H:%M:%SZ"
                ) - dt.datetime.strptime(actions.started, "%Y-%m-%dT%H:%M:%SZ")
        list_vms.append(current_vm)

    return list_vms


def get_migration_report_headers(plan_resource):
    dict_report_info = {}
    dict_stat = {}
    list_vms = []

    migration_type = "WARM" if plan_resource.instance.spec.warm else "COLD"

    list_vms = get_migration_data_from_plan(plan_resource)
    total_data_migrated, total_data_migrated_unit = calc_total_rate(list_vms)
    list_vms = calc_disk_size(list_vms)
    dict_stat = calc_statistics(list_vms, migration_type)
    migration_rate = get_statistics_time(dict_stat, "disk_transfer", "max")

    dict_report_info["report_date"] = str(dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
    dict_report_info["report_id"] = str(uuid.uuid1())
    dict_report_info["test_running_start_time"] = dt.datetime.strptime(
        plan_resource.instance.status.migration.started, "%Y-%m-%dT%H:%M:%SZ"
    )
    dict_report_info["test_running_end_time"] = dt.datetime.strptime(
        plan_resource.instance.status.migration.completed, "%Y-%m-%dT%H:%M:%SZ"
    )
    dict_report_info["source_migration_environment"] = str(plan_resource.instance.spec.provider.source.name)
    dict_report_info["target_migration_environment"] = str(os.getenv("EXECUTER"))
    dict_report_info["mtv_version"] = str(os.getenv("MTV_VERSION"))
    # dict_report_info['iib_number'] = find_iib_number()
    dict_report_info["target_storage"] = str(config.get("storage_class", ""))
    dict_report_info["migration_type"] = str(migration_type)

    if migration_type == "WARM":
        dict_report_info["precopy_interval_in_minutes"] = str(config.get("snapshots_interval", "0"))

    dict_report_info["total_migrated_vms"] = str(len(list_vms))
    dict_report_info["total_plan_duration"] = str(
        dt.datetime.strptime(plan_resource.instance.status.migration.completed, "%Y-%m-%dT%H:%M:%SZ")
        - dt.datetime.strptime(plan_resource.instance.status.migration.started, "%Y-%m-%dT%H:%M:%SZ")
    )
    dict_report_info["total_data_migrated"] = str(total_data_migrated) + total_data_migrated_unit
    #    dict_report_info['data_migration_rate'] = str("{:.1f}".format((float(total_data_migrated)*1024)/float(migration_rate))) + "MB/sec"
    try:
        dict_report_info["data_migration_rate"] = (
            str("{:.1f}".format((float(total_data_migrated) * 1024) / float(migration_rate))) + "MB/sec"
        )
    except ZeroDivisionError:
        dict_report_info["data_migration_rate"] = "can't be calculated, transfer disks time is 00:00:00"

    return list_vms, dict_report_info, dict_stat


def calc_statistics(list_vms, migration_type):
    dict_stat = {}
    dict_keys_cold = ["initialize", "disk_transfer", "image_conversion", "vm_migrate"]
    dict_keys_warm = ["initialize", "disk_transfer", "cutover", "image_conversion", "vm_migrate"]

    dict_keys = dict_keys_cold
    if migration_type == "WARM":
        dict_keys = dict_keys_warm

    for key in dict_keys:
        current_list = [vm.__dict__.get(key) for vm in list_vms]
        dict_stat[key] = {}
        dict_stat[key]["min"], dict_stat[key]["avg"], dict_stat[key]["max"] = find_time_min_max_avg(current_list)

    return dict_stat


def calc_total_rate(list_vms):
    total_data_migrated = 0
    total_data_migrated = sum([float(vm.total_disks_size) for vm in list_vms])

    return calc_disk_size_unit(total_data_migrated)


def calc_disk_size(list_vms):
    for vm in list_vms:
        vm.total_disks_size, vm.disk_size_unit = calc_disk_size_unit(vm.total_disks_size)

    return list_vms


def calc_disk_size_unit(disk_size):
    list_size_units = ["MB", "GB", "TB", "PB"]

    pos = 0
    disk_size = float(disk_size)

    while disk_size >= 1024:
        pos += 1
        disk_size /= 1024

    return str(disk_size), str(list_size_units[pos])


def find_time_min_max_avg(list_val):
    list_val_sec = [val.total_seconds() for val in list_val]

    return min(list_val_sec), mean(list_val_sec), max(list_val_sec)


def find_iib_number():
    t = check_output(
        ["/bin/sh", "-c", "oc get CatalogSource -n openshift-marketplace | grep redhat-osbs | tail -1"],
        stderr=STDOUT,
    )

    return str(t).split(" ")[8]


def get_statistics_time(dict_stat, action, func):
    tmp_dict = dict_stat[action]

    return str(tmp_dict[func])


def write_text_report_file(list_vms, dict_report_info, dict_stat, file_name, migration_type):
    list_titles_cold = [
        "VM_Name",
        "TotalDisks",
        "TotalDisksSize",
        "Initialize",
        "DiskTransfer",
        "ImageConversion",
        "VM_Migrate",
    ]
    list_titles_warm = [
        "VM_Name",
        "TotalDisks",
        "TotalDisksSize",
        "PreCopy",
        "Initialize",
        "DiskTransfer",
        "Cutover",
        "ImageConversion",
        "VM_Migrate",
    ]

    list_titles = list_titles_cold
    if migration_type == "WARM":
        list_titles = list_titles_warm

    f = open(file_name, "w")

    for x, y in dict_report_info.items():
        f.write(str(x).upper() + ": " + str(y) + "\n")

    f.write("\n")

    # Write Titles
    for data in range(len(list_titles)):
        f.write(list_titles[data])
        if ((data + 1) % len(list_titles)) == 1:
            f.write("\t\t\t\t\t\t")
        elif (((data + 1) % len(list_titles)) == 4) and (migration_type == "COLD"):
            f.write("\t")
        elif (
            (((data + 1) % len(list_titles)) == 3)
            or (((data + 1) % len(list_titles)) == 4)
            or (((data + 1) % len(list_titles)) == 7)
        ):
            f.write("\t\t")
        else:
            f.write("\t")
    f.write("\n")

    # Write VMs table
    for vm in list_vms:
        f.write(str(vm.vm_name) + "\t\t")
        f.write(str(vm.total_disks) + "\t\t")
        f.write(str(vm.total_disks_size))
        f.write(str(vm.disk_size_unit) + "\t\t")
        if migration_type == "WARM":
            f.write(str(vm.precopy) + "\t\t")
        f.write(str(vm.initialize) + "\t\t")
        f.write(str(vm.disk_transfer) + "\t\t")
        if migration_type == "WARM":
            f.write(str(vm.cutover) + "\t\t")
        f.write(str(vm.image_conversion) + "\t\t")
        f.write(str(vm.vm_migrate) + "\t\t")
        f.write("\n")
    f.write("\n")

    # Write Stat Summary
    for val in ["min", "avg", "max"]:
        f.write("\t\t\t\t\t" + str(val).upper() + ":\t\t\t\t\t\t")
        if migration_type == "WARM":
            f.write("\t\t")
        for x in dict_stat.values():
            f.write(str(dt.timedelta(seconds=int(x[val]))) + "\t\t")
        f.write("\n")

    f.close()

    print(open(file_name, "r").read())


def write_json_report_file(list_vms, dict_report_info, dict_stat, file_name, migration_type):
    f = open(file_name, "w")
    f.write("{" + "\n")

    for key in dict_report_info:
        f.write('"' + key + '": "' + str(dict_report_info[key]) + '",\n')

    f.write('"vms":' + "\n")
    f.write("[" + "\n")
    for vm in list_vms:
        list_dict = vm.__dict__
        f.write("{" + "\n")
        for x, y in list_dict.items():
            f.write('"' + str(x) + '": "' + str(y) + '",\n')
        f.write("},\n")
    f.write("],\n")

    f.write('"statistic_summary":' + "\n")
    f.write("[" + "\n")
    for x, y in dict_stat.items():
        f.write("{" + "\n")
        for k, v in y.items():
            f.write('"' + str(k) + "_" + str(x) + '": "' + str(dt.timedelta(seconds=int(v))) + '",\n')
        f.write("},\n")
    f.write("],\n")
    f.write("}\n")
    f.close()

    print(open(file_name, "r").read())


def create_migration_scale_report(plan_resource):
    list_vms = []
    dict_report_info = {}
    dict_stat = {}

    list_vms, dict_report_info, dict_stat = get_migration_report_headers(plan_resource)

    # find MigrationType: COLD/WARM
    migration_type = "WARM" if plan_resource.instance.spec.warm else "COLD"

    # Create Report file: "MigrationReport.txt"
    report_file_name = (
        str(dt.datetime.now().strftime("%Y-%m-%d_%H%M")) + "_" + migration_type + "_MigrationReport.txt"
    )  # File name with date
    write_text_report_file(list_vms, dict_report_info, dict_stat, report_file_name, migration_type)

    # Create Report file: "MigrationReport.json"
    report_file_name = (
        str(dt.datetime.now().strftime("%Y-%m-%d_%H%M")) + "_" + migration_type + "_MigrationReport.json"
    )  # File name with date
    write_json_report_file(list_vms, dict_report_info, dict_stat, report_file_name, migration_type)
