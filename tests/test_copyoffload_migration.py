"""
Copy-offload migration tests for MTV.

This module implements tests for copy-offload functionality using the
vsphere-xcopy-volume-populator to migrate VMs with shared storage between
vSphere and OpenShift environments.
"""

import pytest
from ocp_resources.network_map import NetworkMap
from ocp_resources.plan import Plan
from ocp_resources.provider import Provider
from ocp_resources.storage_map import StorageMap
from pytest_testconfig import config as py_config
from simple_logger.logger import get_logger

from utilities.migration_utils import get_cutover_value
from utilities.mtv_migration import (
    create_plan_resource,
    execute_migration,
    get_network_migration_map,
    get_storage_migration_map,
    verify_vm_disk_count,
)
from utilities.naming import sanitize_kubernetes_name
from utilities.post_migration import check_vms


LOGGER = get_logger(__name__)


@pytest.mark.copyoffload
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_thin_migration"])],
    indirect=True,
    ids=["copyoffload-thin"],
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
            destination_namespace=target_namespace,
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
            destination_namespace=target_namespace,
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
    ids=["copyoffload-2tb-vm-snapshots"],
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
    ids=["copyoffload-thick-lazy"],
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
            destination_namespace=target_namespace,
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
    ids=["copyoffload-multi-disk"],
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
            destination_namespace=target_namespace,
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
    ids=["copyoffload-multi-disk-different-path"],
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
            destination_namespace=target_namespace,
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
    ids=["copyoffload-rdm-virtual"],
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
            destination_namespace=target_namespace,
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
    ids=["copyoffload-multi-datastore"],
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
            destination_namespace=target_namespace,
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
    ids=["copyoffload-mixed-datastore"],
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
            destination_namespace=target_namespace,
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
    ids=["copyoffload-independent-persistent"],
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
            destination_namespace=target_namespace,
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
    ids=["copyoffload-independent-nonpersistent"],
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
            destination_namespace=target_namespace,
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
    ids=["copyoffload-10-mixed-disks"],
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
            destination_namespace=target_namespace,
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
    ids=["copyoffload-large-vm"],
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
            destination_namespace=target_namespace,
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
    ids=["copyoffload-nonconforming-name"],
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
            destination_namespace=target_namespace,
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
    ids=["copyoffload-warm"],
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
            destination_namespace=target_namespace,
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
