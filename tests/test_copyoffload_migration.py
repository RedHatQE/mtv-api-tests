"""
Copy-offload migration tests for MTV.

This module implements tests for copy-offload functionality using the
vsphere-xcopy-volume-populator to migrate VMs with shared storage between
vSphere and OpenShift environments.
"""

from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

from ocp_resources.migration import Migration
from ocp_resources.network_map import NetworkMap
from ocp_resources.plan import Plan
from ocp_resources.provider import Provider
from ocp_resources.secret import Secret
from ocp_resources.storage_map import StorageMap
from pytest_testconfig import config as py_config
from simple_logger.logger import get_logger
from timeout_sampler import TimeoutSampler

from libs.base_provider import BaseProvider
from libs.forklift_inventory import ForkliftInventory
from libs.providers.openshift import OCPProvider
from utilities.migration_utils import get_cutover_value
from utilities.mtv_migration import (
    create_plan_resource,
    execute_migration,
    get_network_migration_map,
    get_storage_migration_map,
    verify_vm_disk_count,
    wait_for_migration_complate,
)
from utilities.naming import sanitize_kubernetes_name
from utilities.post_migration import check_vms
from utilities.resources import create_and_store_resource
from utilities.ssh_utils import SSHConnectionManager


LOGGER = get_logger(__name__)

# Error message template for simultaneous migration validation
EARLY_COMPLETION_MSG = (
    "Plan {plan_num} reached {completed_status} before both plans were executing simultaneously. "
    "Other plan status: {other_status}"
)


@pytest.mark.copyoffload
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_thin_migration"])],
    indirect=True,
    ids=["MTV-559:copyoffload-thin"],
)
@pytest.mark.usefixtures("multus_network_name", "copyoffload_config", "setup_copyoffload_ssh", "cleanup_migrated_vms")
class TestCopyoffloadThinMigration:
    """Copy-offload migration test - thin disk."""

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        source_provider_data,
        copyoffload_storage_secret,
    ):
        """Create StorageMap with copy-offload configuration."""
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        storage_class = py_config["storage_class"]

        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            offload_plugin_config=offload_plugin_config,
            access_mode="ReadWriteOnce",
            volume_mode="Block",
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        multus_network_name,
    ):
        """Create NetworkMap resource."""
        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms_names,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        target_namespace,
        source_provider_inventory,
    ):
        """Create MTV Plan CR resource."""
        for vm in prepared_plan["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            copyoffload=prepared_plan.get("copyoffload", False),
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(self, fixture_store, ocp_admin_client, target_namespace):
        """Execute migration."""
        execute_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_check_vms(
        self,
        prepared_plan,
        source_provider,
        destination_provider,
        source_provider_data,
        target_namespace,
        source_vms_namespace,
        source_provider_inventory,
        vm_ssh_connections,
    ):
        """Validate migrated VMs."""
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )


class CopyoffloadSnapshotBase:
    """Base class for copy-offload migration tests with snapshots."""

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        source_provider_data,
        copyoffload_storage_secret,
    ):
        """Create StorageMap with copy-offload configuration after creating snapshots."""
        vm_cfg = prepared_plan["virtual_machines"][0]
        provider_vm_api = prepared_plan["source_vms_data"][vm_cfg["name"]]["provider_vm_api"]

        # Ensure VM is powered on for snapshot creation
        source_provider.start_vm(provider_vm_api)
        source_provider.wait_for_vmware_guest_info(provider_vm_api, timeout=60)

        snapshots_to_create = int(vm_cfg["snapshots"])
        snapshot_prefix = f"{vm_cfg['name']}-{fixture_store['session_uuid']}-snapshot"

        for idx in range(1, snapshots_to_create + 1):
            source_provider.create_snapshot(
                vm=provider_vm_api,
                name=f"{snapshot_prefix}-{idx}",
                description="mtv-api-tests copy-offload snapshots migration test",
                memory=False,
                quiesce=False,
                wait_timeout=60 * 10,
            )

        # Refresh and store snapshots list for post-migration snapshot checks
        vm_cfg["snapshots_before_migration"] = source_provider.vm_dict(provider_vm_api=provider_vm_api)[
            "snapshots_data"
        ]
        assert len(vm_cfg["snapshots_before_migration"]) >= snapshots_to_create, (
            f"Expected at least {snapshots_to_create} snapshots, got {len(vm_cfg['snapshots_before_migration'])}"
        )

        # Cold migration expects VM powered off
        source_provider.stop_vm(provider_vm_api)

        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        storage_class = py_config["storage_class"]

        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            offload_plugin_config=offload_plugin_config,
            access_mode="ReadWriteOnce",
            volume_mode="Block",
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        multus_network_name,
    ):
        """Create NetworkMap resource."""
        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms_names,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        target_namespace,
        source_provider_inventory,
    ):
        """Create MTV Plan CR resource."""
        for vm in prepared_plan["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            copyoffload=prepared_plan.get("copyoffload", False),
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(self, fixture_store, ocp_admin_client, target_namespace):
        """Execute migration."""
        execute_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_check_vms(
        self,
        prepared_plan,
        source_provider,
        destination_provider,
        source_provider_data,
        target_namespace,
        source_vms_namespace,
        source_provider_inventory,
        vm_ssh_connections,
    ):
        """Validate migrated VMs and verify disk count."""
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
        verify_vm_disk_count(
            destination_provider=destination_provider, plan=prepared_plan, target_namespace=target_namespace
        )


@pytest.mark.copyoffload
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_thin_snapshots_migration"])],
    indirect=True,
    ids=["copyoffload-thin-snapshots"],
)
@pytest.mark.skipif(
    py_config.get("source_provider_type") != Provider.ProviderType.VSPHERE,
    reason="Snapshots copy-offload test is only applicable to vSphere source providers",
)
@pytest.mark.usefixtures("multus_network_name", "copyoffload_config", "setup_copyoffload_ssh", "cleanup_migrated_vms")
class TestCopyoffloadThinSnapshotsMigration(CopyoffloadSnapshotBase):
    """Copy-offload migration test - thin disk with snapshots."""


@pytest.mark.copyoffload
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_2tb_vm_snapshots_migration"])],
    indirect=True,
    ids=["MTV-575:copyoffload-2tb-vm-snapshots"],
)
@pytest.mark.skipif(
    py_config.get("source_provider_type") != Provider.ProviderType.VSPHERE,
    reason="Snapshots copy-offload test is only applicable to vSphere source providers",
)
@pytest.mark.usefixtures("multus_network_name", "copyoffload_config", "setup_copyoffload_ssh", "cleanup_migrated_vms")
class TestCopyoffload2TbVmSnapshotsMigration(CopyoffloadSnapshotBase):
    """Copy-offload migration test - 2TB VM with snapshots."""


@pytest.mark.copyoffload
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_thick_lazy_migration"])],
    indirect=True,
    ids=["MTV-580:copyoffload-thick-lazy"],
)
@pytest.mark.usefixtures("multus_network_name", "copyoffload_config", "setup_copyoffload_ssh", "cleanup_migrated_vms")
class TestCopyoffloadThickLazyMigration:
    """Copy-offload migration test - thick lazy disk."""

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        source_provider_data,
        copyoffload_storage_secret,
    ):
        """Create StorageMap with copy-offload configuration."""
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        storage_class = py_config["storage_class"]

        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            offload_plugin_config=offload_plugin_config,
            access_mode="ReadWriteOnce",
            volume_mode="Block",
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        multus_network_name,
    ):
        """Create NetworkMap resource."""
        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms_names,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        target_namespace,
        source_provider_inventory,
    ):
        """Create MTV Plan CR resource."""
        for vm in prepared_plan["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            copyoffload=prepared_plan.get("copyoffload", False),
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(self, fixture_store, ocp_admin_client, target_namespace):
        """Execute migration."""
        execute_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_check_vms(
        self,
        prepared_plan,
        source_provider,
        destination_provider,
        source_provider_data,
        target_namespace,
        source_vms_namespace,
        source_provider_inventory,
        vm_ssh_connections,
    ):
        """Validate migrated VMs."""
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )


@pytest.mark.copyoffload
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_multi_disk_migration"])],
    indirect=True,
    ids=["MTV-561:copyoffload-multi-disk"],
)
@pytest.mark.usefixtures("multus_network_name", "copyoffload_config", "setup_copyoffload_ssh", "cleanup_migrated_vms")
class TestCopyoffloadMultiDiskMigration:
    """Copy-offload migration test - multiple disks."""

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        source_provider_data,
        copyoffload_storage_secret,
    ):
        """Create StorageMap with copy-offload configuration."""
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        storage_class = py_config["storage_class"]

        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            offload_plugin_config=offload_plugin_config,
            access_mode="ReadWriteOnce",
            volume_mode="Block",
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        multus_network_name,
    ):
        """Create NetworkMap resource."""
        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms_names,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        target_namespace,
        source_provider_inventory,
    ):
        """Create MTV Plan CR resource."""
        for vm in prepared_plan["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            copyoffload=prepared_plan.get("copyoffload", False),
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(self, fixture_store, ocp_admin_client, target_namespace):
        """Execute migration."""
        execute_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_check_vms(
        self,
        prepared_plan,
        source_provider,
        destination_provider,
        source_provider_data,
        target_namespace,
        source_vms_namespace,
        source_provider_inventory,
        vm_ssh_connections,
    ):
        """Validate migrated VMs and verify disk count."""
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
        verify_vm_disk_count(
            destination_provider=destination_provider, plan=prepared_plan, target_namespace=target_namespace
        )


@pytest.mark.copyoffload
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_multi_disk_different_path_migration"])],
    indirect=True,
    ids=["MTV-563:copyoffload-multi-disk-different-path"],
)
@pytest.mark.usefixtures("multus_network_name", "copyoffload_config", "setup_copyoffload_ssh", "cleanup_migrated_vms")
class TestCopyoffloadMultiDiskDifferentPathMigration:
    """Copy-offload migration test - multiple disks in different paths."""

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        source_provider_data,
        copyoffload_storage_secret,
    ):
        """Create StorageMap with copy-offload configuration."""
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        storage_class = py_config["storage_class"]

        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            offload_plugin_config=offload_plugin_config,
            access_mode="ReadWriteOnce",
            volume_mode="Block",
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        multus_network_name,
    ):
        """Create NetworkMap resource."""
        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms_names,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        target_namespace,
        source_provider_inventory,
    ):
        """Create MTV Plan CR resource."""
        for vm in prepared_plan["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            copyoffload=prepared_plan.get("copyoffload", False),
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(self, fixture_store, ocp_admin_client, target_namespace):
        """Execute migration."""
        execute_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_check_vms(
        self,
        prepared_plan,
        source_provider,
        destination_provider,
        source_provider_data,
        target_namespace,
        source_vms_namespace,
        source_provider_inventory,
        vm_ssh_connections,
    ):
        """Validate migrated VMs and verify disk count."""
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
        verify_vm_disk_count(
            destination_provider=destination_provider, plan=prepared_plan, target_namespace=target_namespace
        )


@pytest.mark.copyoffload
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_rdm_virtual_disk_migration"])],
    indirect=True,
    ids=["MTV-562:copyoffload-rdm-virtual"],
)
@pytest.mark.usefixtures("multus_network_name", "copyoffload_config", "cleanup_migrated_vms")
class TestCopyoffloadRdmVirtualDiskMigration:
    """Copy-offload migration test - RDM virtual disk."""

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        source_provider_data,
        copyoffload_storage_secret,
    ):
        """Create StorageMap with copy-offload configuration."""
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        storage_class = py_config["storage_class"]

        # Validate RDM LUN is configured
        if "rdm_lun_uuid" not in copyoffload_config_data or not copyoffload_config_data["rdm_lun_uuid"]:
            pytest.fail("rdm_lun_uuid is required in copyoffload configuration for RDM disk tests")

        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            offload_plugin_config=offload_plugin_config,
            access_mode="ReadWriteOnce",
            volume_mode="Block",
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        multus_network_name,
    ):
        """Create NetworkMap resource."""
        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms_names,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        target_namespace,
        source_provider_inventory,
    ):
        """Create MTV Plan CR resource."""
        for vm in prepared_plan["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            copyoffload=prepared_plan.get("copyoffload", False),
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(self, fixture_store, ocp_admin_client, target_namespace):
        """Execute migration."""
        execute_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_check_vms(
        self,
        prepared_plan,
        source_provider,
        destination_provider,
        source_provider_data,
        target_namespace,
        source_vms_namespace,
        source_provider_inventory,
        vm_ssh_connections,
    ):
        """Validate migrated VMs and verify disk count."""
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
        verify_vm_disk_count(
            destination_provider=destination_provider, plan=prepared_plan, target_namespace=target_namespace
        )


@pytest.mark.copyoffload
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_multi_datastore_migration"])],
    indirect=True,
    ids=["MTV-564:copyoffload-multi-datastore"],
)
@pytest.mark.usefixtures("multus_network_name", "copyoffload_config", "cleanup_migrated_vms")
class TestCopyoffloadMultiDatastoreMigration:
    """Copy-offload migration test - multiple datastores."""

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        source_provider_data,
        copyoffload_storage_secret,
    ):
        """Create StorageMap with copy-offload configuration for multiple datastores."""
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        secondary_datastore_id = copyoffload_config_data["secondary_datastore_id"]
        storage_class = py_config["storage_class"]

        # For multi-datastore test, ensure secondary datastore is configured
        if not secondary_datastore_id:
            pytest.fail(
                "Multi-datastore test requires 'secondary_datastore_id' to be configured in copyoffload section."
            )

        LOGGER.info("Multi-datastore migration using primary datastore: %s", datastore_id)
        LOGGER.info("Multi-datastore migration using secondary datastore: %s", secondary_datastore_id)

        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            secondary_datastore_id=secondary_datastore_id,
            offload_plugin_config=offload_plugin_config,
            access_mode="ReadWriteOnce",
            volume_mode="Block",
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        multus_network_name,
    ):
        """Create NetworkMap resource."""
        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms_names,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        target_namespace,
        source_provider_inventory,
    ):
        """Create MTV Plan CR resource."""
        for vm in prepared_plan["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            copyoffload=prepared_plan.get("copyoffload", False),
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(self, fixture_store, ocp_admin_client, target_namespace):
        """Execute migration."""
        execute_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_check_vms(
        self,
        prepared_plan,
        source_provider,
        destination_provider,
        source_provider_data,
        target_namespace,
        source_vms_namespace,
        source_provider_inventory,
        vm_ssh_connections,
    ):
        """Validate migrated VMs and verify disk count."""
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
        verify_vm_disk_count(
            destination_provider=destination_provider, plan=prepared_plan, target_namespace=target_namespace
        )


@pytest.mark.copyoffload
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_mixed_datastore_migration"])],
    indirect=True,
    ids=["MTV-565:copyoffload-mixed-datastore"],
)
@pytest.mark.usefixtures("multus_network_name", "copyoffload_config", "mixed_datastore_config", "cleanup_migrated_vms")
class TestCopyoffloadMixedDatastoreMigration:
    """Copy-offload migration test - mixed XCOPY and non-XCOPY datastores.

    This test validates copy-offload functionality when a VM has:
    - One disk on the primary XCOPY-capable datastore (from the template)
    - One additional disk on a non-XCOPY datastore (standard migration)

    This ensures that copy-offload correctly handles VMs with mixed disk types where
    only some disks can use XCOPY acceleration while others fall back to standard migration.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        source_provider_data,
        copyoffload_storage_secret,
    ):
        """Create StorageMap with copy-offload configuration for mixed datastores."""
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        non_xcopy_datastore_id = copyoffload_config_data["non_xcopy_datastore_id"]
        storage_class = py_config["storage_class"]

        LOGGER.info("Mixed datastore migration using XCOPY datastore: %s", datastore_id)
        LOGGER.info("Mixed datastore migration using non-XCOPY datastore: %s", non_xcopy_datastore_id)

        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            non_xcopy_datastore_id=non_xcopy_datastore_id,
            offload_plugin_config=offload_plugin_config,
            access_mode="ReadWriteOnce",
            volume_mode="Block",
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        multus_network_name,
    ):
        """Create NetworkMap resource."""
        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms_names,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        target_namespace,
        source_provider_inventory,
    ):
        """Create MTV Plan CR resource."""
        for vm in prepared_plan["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            copyoffload=prepared_plan["copyoffload"],
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(self, fixture_store, ocp_admin_client, target_namespace):
        """Execute migration."""
        execute_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_check_vms(
        self,
        prepared_plan,
        source_provider,
        destination_provider,
        source_provider_data,
        target_namespace,
        source_vms_namespace,
        source_provider_inventory,
        vm_ssh_connections,
    ):
        """Validate migrated VMs and verify disk count."""
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
        verify_vm_disk_count(
            destination_provider=destination_provider, plan=prepared_plan, target_namespace=target_namespace
        )


@pytest.mark.copyoffload
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_fallback_large_migration"])],
    indirect=True,
    ids=["MTV-614:copyoffload-fallback-large"],
)
@pytest.mark.usefixtures(
    "multus_network_name",
    "copyoffload_config",
    "mixed_datastore_config",
    "setup_copyoffload_ssh",
    "cleanup_migrated_vms",
)
class TestCopyoffloadFallbackLargeMigration:
    """Copy-offload migration test - large VM with disks on non-XCOPY datastore.

    This test validates copy-offload fallback functionality when VM disks are
    located on a datastore that does NOT support VAAI XCOPY acceleration.
    The VM is configured with:
    - One disk from the template (relocated to non_xcopy_datastore)
    - One additional 100GB disk (also on non_xcopy_datastore)

    The migration should complete successfully using XCOPY's fallback method
    for datastores without VAAI support.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan: dict,
        fixture_store: dict,
        ocp_admin_client: "DynamicClient",
        source_provider: BaseProvider,
        destination_provider: BaseProvider,
        source_provider_inventory: ForkliftInventory,
        target_namespace: str,
        source_provider_data: dict,
        copyoffload_storage_secret: Secret,
    ) -> None:
        """Create StorageMap with copy-offload configuration for non-XCOPY datastore.

        Args:
            prepared_plan: Prepared plan configuration with VM details.
            fixture_store: Shared fixture storage for test resources.
            ocp_admin_client: Kubernetes dynamic client for API operations.
            source_provider: Source provider instance (VMware/RHV/etc).
            destination_provider: Destination provider instance (OpenShift).
            source_provider_inventory: Forklift inventory for source provider.
            target_namespace: Target namespace for migration.
            source_provider_data: Provider configuration data.
            copyoffload_storage_secret: Secret resource for copy-offload storage access.

        Returns:
            None

        Raises:
            AssertionError: If StorageMap creation fails.
        """
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        non_xcopy_datastore_id = copyoffload_config_data["non_xcopy_datastore_id"]
        storage_class = py_config["storage_class"]

        LOGGER.info("Non-XCOPY large VM migration test - 2 disks on non-VAAI datastore")
        LOGGER.info(f"XCOPY datastore (for storage map): {datastore_id}")
        LOGGER.info(f"Non-XCOPY datastore (VM disks location): {non_xcopy_datastore_id}")
        LOGGER.info("VM configuration: template disk + 100GB added disk (both on non_xcopy_datastore)")
        LOGGER.info("Testing fallback behavior for large VM with disks on non-VAAI datastore")

        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            non_xcopy_datastore_id=non_xcopy_datastore_id,
            offload_plugin_config=offload_plugin_config,
            access_mode="ReadWriteOnce",
            volume_mode="Block",
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan: dict,
        fixture_store: dict,
        ocp_admin_client: "DynamicClient",
        source_provider: BaseProvider,
        destination_provider: BaseProvider,
        source_provider_inventory: ForkliftInventory,
        target_namespace: str,
        multus_network_name: dict[str, str],
    ) -> None:
        """Create NetworkMap resource.

        Args:
            prepared_plan: Prepared plan configuration with VM details.
            fixture_store: Shared fixture storage for test resources.
            ocp_admin_client: Kubernetes dynamic client for API operations.
            source_provider: Source provider instance (VMware/RHV/etc).
            destination_provider: Destination provider instance (OpenShift).
            source_provider_inventory: Forklift inventory for source provider.
            target_namespace: Target namespace for migration.
            multus_network_name: Dictionary with Multus network configuration (name and namespace).

        Returns:
            None

        Raises:
            AssertionError: If NetworkMap creation fails.
        """
        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms_names,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan: dict,
        fixture_store: dict,
        ocp_admin_client: "DynamicClient",
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        target_namespace: str,
        source_provider_inventory: ForkliftInventory,
    ) -> None:
        """Create MTV Plan CR resource.

        Args:
            prepared_plan: Prepared plan configuration with VM details.
            fixture_store: Shared fixture storage for test resources.
            ocp_admin_client: Kubernetes dynamic client for API operations.
            source_provider: Source provider instance (VMware/RHV/etc).
            destination_provider: OpenShift provider instance.
            target_namespace: Target namespace for migration.
            source_provider_inventory: Forklift inventory for source provider.

        Returns:
            None

        Raises:
            AssertionError: If Plan creation fails.
        """
        for vm in prepared_plan["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            copyoffload=prepared_plan["copyoffload"],
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(
        self,
        fixture_store: dict,
        ocp_admin_client: "DynamicClient",
        target_namespace: str,
    ) -> None:
        """Execute migration.

        Args:
            fixture_store: Shared fixture storage for test resources.
            ocp_admin_client: Kubernetes dynamic client for API operations.
            target_namespace: Target namespace for migration.

        Returns:
            None

        Raises:
            AssertionError: If migration execution fails.
        """
        execute_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_check_vms(
        self,
        prepared_plan: dict,
        source_provider: BaseProvider,
        destination_provider: BaseProvider,
        source_provider_data: dict,
        target_namespace: str,
        source_vms_namespace: str,
        source_provider_inventory: ForkliftInventory,
        vm_ssh_connections: SSHConnectionManager,
    ) -> None:
        """Validate migrated VMs and verify disk count.

        Args:
            prepared_plan: Prepared plan configuration with VM details.
            source_provider: Source provider instance (VMware/RHV/etc).
            destination_provider: Destination provider instance (OpenShift).
            source_provider_data: Provider configuration data.
            target_namespace: Target namespace for migration.
            source_vms_namespace: Source VMs namespace.
            source_provider_inventory: Forklift inventory for source provider.
            vm_ssh_connections: SSH connection manager for VM validation.

        Returns:
            None

        Raises:
            AssertionError: If VM validation or disk count verification fails.
        """
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
        verify_vm_disk_count(
            destination_provider=destination_provider, plan=prepared_plan, target_namespace=target_namespace
        )


@pytest.mark.copyoffload
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_independent_persistent_disk_migration"])],
    indirect=True,
    ids=["MTV-567:copyoffload-independent-persistent"],
)
@pytest.mark.usefixtures("multus_network_name", "copyoffload_config", "cleanup_migrated_vms")
class TestCopyoffloadIndependentPersistentDiskMigration:
    """Copy-offload migration test - independent persistent disk."""

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        source_provider_data,
        copyoffload_storage_secret,
    ):
        """Create StorageMap with copy-offload configuration."""
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        storage_class = py_config["storage_class"]

        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            offload_plugin_config=offload_plugin_config,
            access_mode="ReadWriteOnce",
            volume_mode="Block",
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        multus_network_name,
    ):
        """Create NetworkMap resource."""
        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms_names,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        target_namespace,
        source_provider_inventory,
    ):
        """Create MTV Plan CR resource."""
        for vm in prepared_plan["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            copyoffload=prepared_plan.get("copyoffload", False),
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(self, fixture_store, ocp_admin_client, target_namespace):
        """Execute migration."""
        execute_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_check_vms(
        self,
        prepared_plan,
        source_provider,
        destination_provider,
        source_provider_data,
        target_namespace,
        source_vms_namespace,
        source_provider_inventory,
        vm_ssh_connections,
    ):
        """Validate migrated VMs and verify disk count."""
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
        verify_vm_disk_count(
            destination_provider=destination_provider, plan=prepared_plan, target_namespace=target_namespace
        )


@pytest.mark.copyoffload
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_independent_nonpersistent_disk_migration"])],
    indirect=True,
    ids=["MTV-568:copyoffload-independent-nonpersistent"],
)
@pytest.mark.usefixtures("multus_network_name", "copyoffload_config", "cleanup_migrated_vms")
class TestCopyoffloadIndependentNonpersistentDiskMigration:
    """Copy-offload migration test - independent non-persistent disk."""

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        source_provider_data,
        copyoffload_storage_secret,
    ):
        """Create StorageMap with copy-offload configuration."""
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        storage_class = py_config["storage_class"]

        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            offload_plugin_config=offload_plugin_config,
            access_mode="ReadWriteOnce",
            volume_mode="Block",
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        multus_network_name,
    ):
        """Create NetworkMap resource."""
        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms_names,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        target_namespace,
        source_provider_inventory,
    ):
        """Create MTV Plan CR resource."""
        for vm in prepared_plan["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            copyoffload=prepared_plan.get("copyoffload", False),
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(self, fixture_store, ocp_admin_client, target_namespace):
        """Execute migration."""
        execute_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_check_vms(
        self,
        prepared_plan,
        source_provider,
        destination_provider,
        source_provider_data,
        target_namespace,
        source_vms_namespace,
        source_provider_inventory,
        vm_ssh_connections,
    ):
        """Validate migrated VMs and verify disk count."""
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
        verify_vm_disk_count(
            destination_provider=destination_provider, plan=prepared_plan, target_namespace=target_namespace
        )


@pytest.mark.copyoffload
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_10_mixed_disks_migration"])],
    indirect=True,
    ids=["MTV-573:copyoffload-10-mixed-disks"],
)
@pytest.mark.usefixtures("multus_network_name", "copyoffload_config", "cleanup_migrated_vms")
class TestCopyoffload10MixedDisksMigration:
    """Copy-offload migration test - 10 mixed disks (thin/thick)."""

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        source_provider_data,
        copyoffload_storage_secret,
    ):
        """Create StorageMap with copy-offload configuration."""
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        storage_class = py_config["storage_class"]

        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            offload_plugin_config=offload_plugin_config,
            access_mode="ReadWriteOnce",
            volume_mode="Block",
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        multus_network_name,
    ):
        """Create NetworkMap resource."""
        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms_names,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        target_namespace,
        source_provider_inventory,
    ):
        """Create MTV Plan CR resource."""
        for vm in prepared_plan["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            copyoffload=prepared_plan.get("copyoffload", False),
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(self, fixture_store, ocp_admin_client, target_namespace):
        """Execute migration."""
        execute_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_check_vms(
        self,
        prepared_plan,
        source_provider,
        destination_provider,
        source_provider_data,
        target_namespace,
        source_vms_namespace,
        source_provider_inventory,
        vm_ssh_connections,
    ):
        """Validate migrated VMs and verify disk count."""
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
        verify_vm_disk_count(
            destination_provider=destination_provider, plan=prepared_plan, target_namespace=target_namespace
        )


@pytest.mark.copyoffload
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_large_vm_migration"])],
    indirect=True,
    ids=["MTV-600:copyoffload-large-vm"],
)
@pytest.mark.usefixtures("multus_network_name", "copyoffload_config", "cleanup_migrated_vms")
class TestCopyoffloadLargeVmMigration:
    """Copy-offload migration test - large VM (1TB)."""

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        source_provider_data,
        copyoffload_storage_secret,
    ):
        """Create StorageMap with copy-offload configuration."""
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        storage_class = py_config["storage_class"]

        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            offload_plugin_config=offload_plugin_config,
            access_mode="ReadWriteOnce",
            volume_mode="Block",
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        multus_network_name,
    ):
        """Create NetworkMap resource."""
        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms_names,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        target_namespace,
        source_provider_inventory,
    ):
        """Create MTV Plan CR resource."""
        for vm in prepared_plan["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            copyoffload=prepared_plan.get("copyoffload", False),
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(self, fixture_store, ocp_admin_client, target_namespace):
        """Execute migration."""
        execute_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_check_vms(
        self,
        prepared_plan,
        source_provider,
        destination_provider,
        source_provider_data,
        target_namespace,
        source_vms_namespace,
        source_provider_inventory,
        vm_ssh_connections,
    ):
        """Validate migrated VMs and verify disk count."""
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
        verify_vm_disk_count(
            destination_provider=destination_provider, plan=prepared_plan, target_namespace=target_namespace
        )


@pytest.mark.copyoffload
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_nonconforming_name_migration"])],
    indirect=True,
    ids=["MTV-579:copyoffload-nonconforming-name"],
)
@pytest.mark.usefixtures("multus_network_name", "copyoffload_config", "setup_copyoffload_ssh", "cleanup_migrated_vms")
class TestCopyoffloadNonconformingNameMigration:
    """
    Copy-offload migration test - VM with non-conforming name.

    This test validates that MTV/Forklift properly handles VMs with names that don't
    conform to Kubernetes naming conventions (e.g., containing capital letters and
    underscores). The operator should automatically convert the source VM name to a
    valid Kubernetes name (lowercase, hyphens instead of underscores) when creating
    the destination VM in OpenShift.

    Test Scenario:
    - Source VM name: "XCopy_Test_VM_CAPS" (has capitals and underscores)
    - Expected destination: Valid Kubernetes name (e.g., "xcopy-test-vm-caps")

    Requirements:
    - vSphere provider with VMs on XCOPY-capable storage
    - Shared storage between vSphere and OpenShift (NetApp ONTAP, Hitachi Vantara, etc.)
    - Storage credentials via environment variables or .providers.json config
    - ForkliftController with feature_copy_offload: "true" (must be pre-configured)

    See .providers.json.example for supported vendors and configuration.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        source_provider_data,
        copyoffload_storage_secret,
    ):
        """Create StorageMap with copy-offload configuration."""
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        storage_class = py_config["storage_class"]

        LOGGER.info("Starting copy-offload migration test with non-conforming VM name")
        vm_cfg = prepared_plan["virtual_machines"][0]
        source_vm_name = vm_cfg.get("clone_name") or vm_cfg["name"]
        LOGGER.info("Source VM name: %s (contains capitals and underscores)", source_vm_name)
        LOGGER.info("Datastore: %s, Storage vendor: %s", datastore_id, storage_vendor_product)

        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            offload_plugin_config=offload_plugin_config,
            access_mode="ReadWriteOnce",
            volume_mode="Block",
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        multus_network_name,
    ):
        """Create NetworkMap resource."""
        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms_names,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        target_namespace,
        source_provider_inventory,
    ):
        """Create MTV Plan CR resource."""
        for vm in prepared_plan["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            copyoffload=prepared_plan.get("copyoffload", False),
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(self, fixture_store, ocp_admin_client, target_namespace):
        """Execute migration."""
        LOGGER.info("Executing copy-offload migration with non-conforming name")
        execute_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_check_vms(
        self,
        prepared_plan,
        source_provider,
        destination_provider,
        source_provider_data,
        target_namespace,
        source_vms_namespace,
        source_provider_inventory,
        vm_ssh_connections,
    ):
        """Validate migrated VMs and verify name sanitization."""
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )

        # Verify that the destination VM was created with the sanitized Kubernetes-compliant name.
        # This is an explicit test of the name sanitization behavior, separate from the general
        # VM validation done by check_vms(). We need this additional check to verify the specific
        # test scenario: that non-conforming names (capitals, underscores) are properly sanitized.
        vm_cfg = prepared_plan["virtual_machines"][0]
        # vm_cfg["name"] contains the cloned VM name (mutated by prepared_plan fixture).
        # source_vms_data is keyed by this same name to store the provider API object.
        provider_vm_api = prepared_plan["source_vms_data"][vm_cfg["name"]]["provider_vm_api"]
        actual_source_vm_name = provider_vm_api.name
        # MTV should sanitize the source VM name to create the destination VM name
        expected_destination_name = sanitize_kubernetes_name(actual_source_vm_name)
        LOGGER.info(
            "Verifying destination VM name sanitization: '%s' -> '%s'",
            actual_source_vm_name,
            expected_destination_name,
        )

        # Use destination_provider to verify the VM exists with the sanitized name
        destination_vm = destination_provider.vm_dict(
            name=expected_destination_name,  # Use sanitized name to look up the VM
            namespace=target_namespace,
        )

        # Assert the VM exists and has the expected sanitized name
        actual_destination_name = destination_vm["name"]
        assert actual_destination_name == expected_destination_name, (
            f"Destination VM name mismatch!\n"
            f"  Source VM name: '{actual_source_vm_name}'\n"
            f"  Expected sanitized name: '{expected_destination_name}'\n"
            f"  Actual destination name: '{actual_destination_name}'\n"
            f"  This indicates the MTV operator did not properly sanitize the VM name."
        )
        LOGGER.info("✓ Destination VM name correctly sanitized to: '%s'", actual_destination_name)

        # Verify that the VM disk was migrated successfully
        verify_vm_disk_count(
            destination_provider=destination_provider, plan=prepared_plan, target_namespace=target_namespace
        )


@pytest.mark.copyoffload
@pytest.mark.warm
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_warm_migration"])],
    indirect=True,
    ids=["MTV-577:copyoffload-warm"],
)
@pytest.mark.usefixtures(
    "multus_network_name", "precopy_interval_forkliftcontroller", "copyoffload_config", "cleanup_migrated_vms"
)
class TestCopyoffloadWarmMigration:
    """Copy-offload warm migration test."""

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        source_provider_data,
        copyoffload_storage_secret,
    ):
        """Create StorageMap with copy-offload configuration."""
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        storage_class = py_config["storage_class"]

        LOGGER.info("Starting copy-offload warm migration test")
        LOGGER.info("Datastore: %s, Storage vendor: %s", datastore_id, storage_vendor_product)

        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            offload_plugin_config=offload_plugin_config,
            access_mode="ReadWriteOnce",
            volume_mode="Block",
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        multus_network_name,
    ):
        """Create NetworkMap resource."""
        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms_names,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        target_namespace,
        source_provider_inventory,
    ):
        """Create MTV Plan CR resource."""
        for vm in prepared_plan["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            copyoffload=prepared_plan.get("copyoffload", False),
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(self, fixture_store, ocp_admin_client, target_namespace):
        """Execute warm migration with cutover."""
        LOGGER.info("Executing warm migration with copy-offload acceleration")
        execute_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
            cut_over=get_cutover_value(),
        )

    def test_check_vms(
        self,
        prepared_plan,
        source_provider,
        destination_provider,
        source_provider_data,
        target_namespace,
        source_vms_namespace,
        source_provider_inventory,
        vm_ssh_connections,
    ):
        """Validate migrated VMs and verify disk count."""
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
        verify_vm_disk_count(
            destination_provider=destination_provider, plan=prepared_plan, target_namespace=target_namespace
        )


@pytest.mark.copyoffload
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_scale_migration"])],
    indirect=True,
    ids=["MTV-572:copyoffload-scale"],
)
@pytest.mark.usefixtures("multus_network_name", "copyoffload_config", "setup_copyoffload_ssh", "cleanup_migrated_vms")
class TestCopyoffloadScaleMigration:
    """Copy-offload migration test - scale (5 VMs)."""

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        source_provider_data,
        copyoffload_storage_secret,
    ):
        """Create StorageMap with copy-offload configuration."""
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        storage_class = py_config["storage_class"]

        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            offload_plugin_config=offload_plugin_config,
            access_mode="ReadWriteOnce",
            volume_mode="Block",
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        multus_network_name,
    ):
        """Create NetworkMap resource."""
        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms_names,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        target_namespace,
        source_provider_inventory,
    ):
        """Create MTV Plan CR resource."""
        for vm in prepared_plan["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            copyoffload=prepared_plan.get("copyoffload", False),
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(self, fixture_store, ocp_admin_client, target_namespace):
        """Execute migration."""
        execute_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_check_vms(
        self,
        prepared_plan,
        source_provider,
        destination_provider,
        source_provider_data,
        target_namespace,
        source_vms_namespace,
        source_provider_inventory,
        vm_ssh_connections,
    ):
        """Validate migrated VMs and verify disk count."""
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
        verify_vm_disk_count(
            destination_provider=destination_provider, plan=prepared_plan, target_namespace=target_namespace
        )


@pytest.mark.copyoffload
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_simultaneous_copyoffload_migrations"])],
    indirect=True,
    ids=["MTV-574:simultaneous-copyoffload"],
)
@pytest.mark.usefixtures("copyoffload_config", "setup_copyoffload_ssh", "cleanup_migrated_vms")
class TestSimultaneousCopyoffloadMigrations:
    """Test simultaneous execution of two copyoffload migration plans."""

    storage_map_1: StorageMap
    network_map_1: NetworkMap
    plan_resource_1: Plan

    storage_map_2: StorageMap
    network_map_2: NetworkMap
    plan_resource_2: Plan

    def test_create_storagemap_plan1(
        self,
        prepared_plan_1: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: "DynamicClient",
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_inventory: ForkliftInventory,
        target_namespace: str,
        source_provider_data: dict[str, Any],
        copyoffload_storage_secret: Secret,
    ) -> None:
        """Create StorageMap with copy-offload configuration for first plan.

        Args:
            prepared_plan_1: Prepared plan configuration for plan 1
            fixture_store: Fixture store for resource tracking
            ocp_admin_client: OpenShift admin client
            source_provider: Source provider instance
            destination_provider: Destination provider instance
            source_provider_inventory: Source provider inventory
            target_namespace: Target namespace for resources
            source_provider_data: Source provider configuration data
            copyoffload_storage_secret: Secret for copy-offload storage access

        Returns:
            None

        Raises:
            AssertionError: If StorageMap creation fails
        """
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        storage_class = py_config["storage_class"]

        vms_names = [vm["name"] for vm in prepared_plan_1["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map_1 = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            offload_plugin_config=offload_plugin_config,
            access_mode="ReadWriteOnce",
            volume_mode="Block",
        )
        assert self.storage_map_1, "StorageMap creation failed for plan 1"

    def test_create_storagemap_plan2(
        self,
        prepared_plan_2: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: "DynamicClient",
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_inventory: ForkliftInventory,
        target_namespace: str,
        source_provider_data: dict[str, Any],
        copyoffload_storage_secret: Secret,
    ) -> None:
        """Create StorageMap with copy-offload configuration for second plan.

        Args:
            prepared_plan_2: Prepared plan configuration for plan 2
            fixture_store: Fixture store for resource tracking
            ocp_admin_client: OpenShift admin client
            source_provider: Source provider instance
            destination_provider: Destination provider instance
            source_provider_inventory: Source provider inventory
            target_namespace: Target namespace for resources
            source_provider_data: Source provider configuration data
            copyoffload_storage_secret: Secret for copy-offload storage access

        Returns:
            None

        Raises:
            AssertionError: If StorageMap creation fails
        """
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        storage_class = py_config["storage_class"]

        vms_names = [vm["name"] for vm in prepared_plan_2["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map_2 = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            offload_plugin_config=offload_plugin_config,
            access_mode="ReadWriteOnce",
            volume_mode="Block",
        )
        assert self.storage_map_2, "StorageMap creation failed for plan 2"

    def test_create_networkmap_plan1(
        self,
        prepared_plan_1: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: "DynamicClient",
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_inventory: ForkliftInventory,
        target_namespace: str,
        multus_network_name: dict[str, str],
    ) -> None:
        """Create NetworkMap resource for first plan.

        Args:
            prepared_plan_1: Prepared plan configuration for plan 1
            fixture_store: Fixture store for resource tracking
            ocp_admin_client: OpenShift admin client
            source_provider: Source provider instance
            destination_provider: Destination provider instance
            source_provider_inventory: Source provider inventory
            target_namespace: Target namespace for resources
            multus_network_name: Network mapping from source to destination networks

        Returns:
            None

        Raises:
            AssertionError: If NetworkMap creation fails
        """
        vms_names = [vm["name"] for vm in prepared_plan_1["virtual_machines"]]

        self.__class__.network_map_1 = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            target_namespace=target_namespace,
            multus_network_name=multus_network_name,
            vms=vms_names,
        )
        assert self.network_map_1, "NetworkMap creation failed for plan 1"

    def test_create_networkmap_plan2(
        self,
        prepared_plan_2: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: "DynamicClient",
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_inventory: ForkliftInventory,
        target_namespace: str,
        multus_network_name: dict[str, str],
    ) -> None:
        """Create NetworkMap resource for second plan.

        Args:
            prepared_plan_2: Prepared plan configuration for plan 2
            fixture_store: Fixture store for resource tracking
            ocp_admin_client: OpenShift admin client
            source_provider: Source provider instance
            destination_provider: Destination provider instance
            source_provider_inventory: Source provider inventory
            target_namespace: Target namespace for resources
            multus_network_name: Network mapping from source to destination networks

        Returns:
            None

        Raises:
            AssertionError: If NetworkMap creation fails
        """
        vms_names = [vm["name"] for vm in prepared_plan_2["virtual_machines"]]

        self.__class__.network_map_2 = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            target_namespace=target_namespace,
            multus_network_name=multus_network_name,
            vms=vms_names,
        )
        assert self.network_map_2, "NetworkMap creation failed for plan 2"

    def test_create_plan1(
        self,
        prepared_plan_1: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: "DynamicClient",
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        target_namespace: str,
        source_provider_inventory: ForkliftInventory,
    ) -> None:
        """Create first MTV Plan CR resource for copy-offload.

        Args:
            prepared_plan_1: Prepared plan configuration for plan 1
            fixture_store: Fixture store for resource tracking
            ocp_admin_client: OpenShift admin client
            source_provider: Source provider instance
            destination_provider: Destination provider instance
            target_namespace: Target namespace for resources
            source_provider_inventory: Source provider inventory

        Returns:
            None

        Raises:
            AssertionError: If Plan creation fails
        """
        for vm in prepared_plan_1["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource_1 = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map_1,
            network_map=self.network_map_1,
            virtual_machines_list=prepared_plan_1["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan_1.get("warm_migration", False),
            copyoffload=prepared_plan_1.get("copyoffload", False),
            test_name="simultaneous-copyoffload-plan1",
        )
        assert self.plan_resource_1, "Plan creation failed for plan 1"

    def test_create_plan2(
        self,
        prepared_plan_2: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: "DynamicClient",
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        target_namespace: str,
        source_provider_inventory: ForkliftInventory,
    ) -> None:
        """Create second MTV Plan CR resource for copy-offload.

        Args:
            prepared_plan_2: Prepared plan configuration for plan 2
            fixture_store: Fixture store for resource tracking
            ocp_admin_client: OpenShift admin client
            source_provider: Source provider instance
            destination_provider: Destination provider instance
            target_namespace: Target namespace for resources
            source_provider_inventory: Source provider inventory

        Returns:
            None

        Raises:
            AssertionError: If Plan creation fails
        """
        for vm in prepared_plan_2["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource_2 = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map_2,
            network_map=self.network_map_2,
            virtual_machines_list=prepared_plan_2["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan_2.get("warm_migration", False),
            copyoffload=prepared_plan_2.get("copyoffload", False),
            test_name="simultaneous-copyoffload-plan2",
        )
        assert self.plan_resource_2, "Plan creation failed for plan 2"

    def test_migrate_vms_simultaneously(
        self,
        fixture_store: dict[str, Any],
        ocp_admin_client: "DynamicClient",
        target_namespace: str,
    ) -> None:
        """Execute both copyoffload migrations simultaneously.

        Args:
            fixture_store: Fixture store for resource tracking
            ocp_admin_client: OpenShift admin client
            target_namespace: Target namespace for migrations

        Returns:
            None

        Raises:
            AssertionError: If migration execution or completion fails or if simultaneous execution is not validated
        """
        LOGGER.info("Starting simultaneous execution of both copyoffload migration plans")

        # Create Migration CR for first plan
        migration_1 = create_and_store_resource(
            client=ocp_admin_client,
            fixture_store=fixture_store,
            resource=Migration,
            namespace=target_namespace,
            plan_name=self.plan_resource_1.name,
            plan_namespace=self.plan_resource_1.namespace,
            test_name="simultaneous-copyoffload-migration1",
        )
        LOGGER.info(f"Created Migration CR for plan 1: {migration_1.name}")

        # Create Migration CR for second plan
        migration_2 = create_and_store_resource(
            client=ocp_admin_client,
            fixture_store=fixture_store,
            resource=Migration,
            namespace=target_namespace,
            plan_name=self.plan_resource_2.name,
            plan_namespace=self.plan_resource_2.namespace,
            test_name="simultaneous-copyoffload-migration2",
        )
        LOGGER.info(f"Created Migration CR for plan 2: {migration_2.name}")

        # Validate both migrations are executing simultaneously before either completes
        LOGGER.info("Validating both migrations enter executing state simultaneously")
        plan_1_executing = False
        plan_2_executing = False
        both_executing_validated = False

        def _check_plan_status(plan: Plan) -> str:
            """Check if plan is executing, succeeded, or failed.

            Args:
                plan: The Plan resource to check status for

            Returns:
                str: "Executing" if migration is in progress, Plan.Status.SUCCEEDED if completed successfully,
                    or Plan.Status.FAILED if migration failed

            Raises:
                AttributeError: If plan.instance.status.conditions is not accessible
                KeyError: If condition dictionary is missing required keys (category, status, type)
            """
            for cond in plan.instance.status.conditions:
                if cond["category"] == "Advisory" and cond["status"] == Plan.Condition.Status.TRUE:
                    cond_type = cond["type"]
                    if cond_type in (Plan.Status.SUCCEEDED, Plan.Status.FAILED):
                        return cond_type
            return "Executing"

        # Poll both plans to ensure both reach executing state before either completes
        for sample in TimeoutSampler(
            func=lambda: (_check_plan_status(self.plan_resource_1), _check_plan_status(self.plan_resource_2)),
            sleep=2,
            wait_timeout=120,
        ):
            status_1, status_2 = sample

            # Check if plan 1 is executing
            if status_1 == "Executing":
                if not plan_1_executing:
                    LOGGER.info(f"Plan 1 '{self.plan_resource_1.name}' is now executing")
                plan_1_executing = True

            # Check if plan 2 is executing
            if status_2 == "Executing":
                if not plan_2_executing:
                    LOGGER.info(f"Plan 2 '{self.plan_resource_2.name}' is now executing")
                plan_2_executing = True

            # Assert both are executing simultaneously
            if plan_1_executing and plan_2_executing and not both_executing_validated:
                LOGGER.info("SUCCESS: Both migrations are executing simultaneously")
                both_executing_validated = True
                break

            # If either plan completed before both were executing, fail the test
            if status_1 in (Plan.Status.SUCCEEDED, Plan.Status.FAILED) and not both_executing_validated:
                raise AssertionError(
                    EARLY_COMPLETION_MSG.format(plan_num=1, completed_status=status_1, other_status=status_2)
                )
            if status_2 in (Plan.Status.SUCCEEDED, Plan.Status.FAILED) and not both_executing_validated:
                raise AssertionError(
                    EARLY_COMPLETION_MSG.format(plan_num=2, completed_status=status_2, other_status=status_1)
                )

        assert both_executing_validated, "Failed to validate both migrations executing simultaneously"

        # Wait for both migrations to complete
        LOGGER.info("Waiting for both copyoffload migrations to complete")
        wait_for_migration_complate(plan=self.plan_resource_1)
        LOGGER.info("Copyoffload migration 1 completed")

        wait_for_migration_complate(plan=self.plan_resource_2)
        LOGGER.info("Copyoffload migration 2 completed")

    def test_check_vms_plan1(
        self,
        prepared_plan_1: dict[str, Any],
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_data: dict[str, Any],
        target_namespace: str,
        source_vms_namespace: str,
        source_provider_inventory: ForkliftInventory,
        vm_ssh_connections: SSHConnectionManager | None,
    ) -> None:
        """Validate migrated VMs from first plan.

        Args:
            prepared_plan_1: Prepared plan configuration for plan 1
            source_provider: Source provider instance
            destination_provider: Destination provider instance
            source_provider_data: Source provider configuration data
            target_namespace: Target namespace where VMs were migrated
            source_vms_namespace: Source VMs namespace
            source_provider_inventory: Source provider inventory
            vm_ssh_connections: SSH connections manager for VMs

        Returns:
            None

        Raises:
            AssertionError: If VM validation fails or disk counts mismatch
        """
        check_vms(
            plan=prepared_plan_1,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map_1,
            storage_map_resource=self.storage_map_1,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
        verify_vm_disk_count(
            destination_provider=destination_provider, plan=prepared_plan_1, target_namespace=target_namespace
        )

    def test_check_vms_plan2(
        self,
        prepared_plan_2: dict[str, Any],
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_data: dict[str, Any],
        target_namespace: str,
        source_vms_namespace: str,
        source_provider_inventory: ForkliftInventory,
        vm_ssh_connections: SSHConnectionManager | None,
    ) -> None:
        """Validate migrated VMs from second plan.

        Args:
            prepared_plan_2: Prepared plan configuration for plan 2
            source_provider: Source provider instance
            destination_provider: Destination provider instance
            source_provider_data: Source provider configuration data
            target_namespace: Target namespace where VMs were migrated
            source_vms_namespace: Source VMs namespace
            source_provider_inventory: Source provider inventory
            vm_ssh_connections: SSH connections manager for VMs

        Returns:
            None

        Raises:
            AssertionError: If VM validation fails or disk counts mismatch
        """
        check_vms(
            plan=prepared_plan_2,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map_2,
            storage_map_resource=self.storage_map_2,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
        verify_vm_disk_count(
            destination_provider=destination_provider, plan=prepared_plan_2, target_namespace=target_namespace
        )
