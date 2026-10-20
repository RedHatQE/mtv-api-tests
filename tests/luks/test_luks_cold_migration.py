from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from ocp_resources.network_map import NetworkMap
from ocp_resources.plan import Plan
from ocp_resources.storage_map import StorageMap
from pytest_testconfig import config as py_config

from exceptions.exceptions import MigrationPlanExecError
from utilities.mtv_migration import (
    create_plan_resource,
    execute_migration,
    get_network_migration_map,
    get_storage_migration_map,
)
from utilities.post_migration import check_vms, verify_luks_encryption

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient
    from libs.base_provider import BaseProvider
    from libs.forklift_inventory import ForkliftInventory
    from libs.providers.openshift import OCPProvider
    from utilities.ssh_utils import SSHConnectionManager


class LuksColdMigrationBase:
    """Shared setup for LUKS cold migration tests.

    Provides common storagemap, networkmap, and plan creation methods.
    Subclasses define scenario-specific migration execution and validation.

    VM requirements:
        The source VM must have a LUKS-encrypted partition (e.g. sda3 with crypto_LUKS)
        and a key file (e.g. /etc/luks-key) configured in /etc/crypttab for unattended
        boot. Without a key file, the VM prompts for the passphrase interactively on
        boot, which blocks SSH access after migration.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        source_provider: "BaseProvider",
        destination_provider: "BaseProvider",
        ocp_admin_client: "DynamicClient",
        target_namespace: str,
        source_provider_inventory: "ForkliftInventory",
    ) -> None:
        """Create StorageMap resource.

        Args:
            prepared_plan (dict[str, Any]): Test plan configuration with VM details.
            fixture_store (dict[str, Any]): Resource tracking dictionary.
            source_provider (BaseProvider): Source provider connection.
            destination_provider (BaseProvider): Destination provider connection.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Target namespace for migration resources.
            source_provider_inventory (ForkliftInventory): Source provider inventory.

        Raises:
            AssertionError: If StorageMap creation fails.
        """
        vms = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            target_namespace=target_namespace,
            vms=vms,
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        source_provider: "BaseProvider",
        destination_provider: "BaseProvider",
        ocp_admin_client: "DynamicClient",
        target_namespace: str,
        source_provider_inventory: "ForkliftInventory",
        multus_network_name: dict[str, str],
    ) -> None:
        """Create NetworkMap resource.

        Args:
            prepared_plan (dict[str, Any]): Test plan configuration with VM details.
            fixture_store (dict[str, Any]): Resource tracking dictionary.
            source_provider (BaseProvider): Source provider connection.
            destination_provider (BaseProvider): Destination provider connection.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Target namespace for migration resources.
            source_provider_inventory (ForkliftInventory): Source provider inventory.
            multus_network_name (dict[str, str]): Multus network name for network mapping.

        Raises:
            AssertionError: If NetworkMap creation fails.
        """
        vms = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        source_provider: "BaseProvider",
        destination_provider: "OCPProvider",
        ocp_admin_client: "DynamicClient",
        target_namespace: str,
        luks_vm_specs: list[dict[str, Any]],
    ) -> None:
        """Create MTV Plan with LUKS decryption secrets.

        Uses the luks_vm_specs fixture which resolves passphrases, populates VM IDs,
        and creates K8s Secrets per-VM. This method only handles plan creation.

        Args:
            prepared_plan (dict[str, Any]): Test plan configuration with VM details.
            fixture_store (dict[str, Any]): Resource tracking dictionary.
            source_provider (BaseProvider): Source provider connection.
            destination_provider (OCPProvider): Destination provider connection.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Target namespace for migration resources.
            luks_vm_specs (list[dict[str, Any]]): VM specs with LUKS secrets and IDs injected.

        Raises:
            AssertionError: If Plan creation fails.
        """
        self.__class__.plan_resource = create_plan_resource(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            ocp_admin_client=ocp_admin_client,
            target_namespace=target_namespace,
            virtual_machines_list=luks_vm_specs,
            warm_migration=prepared_plan.get("warm_migration", False),
        )
        assert self.plan_resource, "Plan creation failed"


@pytest.mark.vsphere
@pytest.mark.tier1
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_luks_cold_migration"])],
    indirect=True,
    ids=["luks-cold-correct-key"],
)
@pytest.mark.usefixtures("cleanup_migrated_vms")
@pytest.mark.incremental
class TestLuksColdMigration(LuksColdMigrationBase):
    """Cold-migrate a VM with a LUKS-encrypted disk using the correct passphrase and verify encryption survives.

    Purpose/Regression:
        A VM with a LUKS-encrypted partition must migrate when the correct
        decryption passphrase is supplied to the Plan, and the disk must
        remain LUKS-encrypted on the destination. The pass signal is a
        successful migration plus an active crypto_LUKS device inside the
        migrated guest. There is no induced failure.

    Prerequisites:
        Register and connect a vSphere source provider and an OpenShift
        destination with MTV installed, with accessible destination storage
        and network. Use a disposable RHEL (or similar) VM with a
        LUKS-encrypted partition (e.g. crypto_LUKS on the root or a data
        partition) and a key file configured in /etc/crypttab so the VM
        boots unattended, with SSH access and known guest credentials. Know
        the partition's correct LUKS passphrase. Have permission to create
        a per-VM passphrase Secret, maps, and a Plan in a target namespace.

    Test plan:
        1. Prepare the disposable VM, wait until it appears in Forklift
           inventory, and create a Kubernetes Secret holding the VM's correct
           LUKS passphrase for the Plan to reference.
        2. Create a StorageMap for the VM's disks and a NetworkMap for its
           network. Confirm both are created.
        3. Create a cold Plan for the VM referencing the passphrase Secret.
           Check the Plan's Ready condition is True.
        4. Run the migration and check it reaches Succeeded.
        5. Log in to the migrated VM and confirm a device with filesystem
           type crypto_LUKS is present (e.g. via lsblk), proving the
           encryption survived the migration.
        6. Run the standard VM validation: power state, connectivity, and
           CPU, memory, network, and storage equivalence with the source.
           After the checks, delete the migrated VM; the passphrase Secret,
           maps, and Plan are removed with the run.

    Expected result:
        The migration succeeds with the correct passphrase, and the migrated
        VM still has an active LUKS-encrypted device and validates against
        the source. On failure, inspect the migration CR status for the
        failed step, the Plan's referenced passphrase Secret, and the guest's
        block device layout (lsblk) for the crypto_LUKS entry.
    """

    def test_migrate_vms(
        self,
        fixture_store: dict[str, Any],
        ocp_admin_client: "DynamicClient",
        target_namespace: str,
    ) -> None:
        """Execute cold migration with LUKS decryption.

        Args:
            fixture_store (dict[str, Any]): Resource tracking dictionary.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Target namespace for migration resources.

        Raises:
            MigrationPlanExecError: If migration execution fails.
        """
        execute_migration(
            fixture_store=fixture_store,
            ocp_admin_client=ocp_admin_client,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_verify_luks_encryption(
        self,
        prepared_plan: dict[str, Any],
        vm_ssh_connections: "SSHConnectionManager",
        source_provider_data: dict[str, Any],
    ) -> None:
        """Verify LUKS encryption is active on migrated VM.

        SSHs into the migrated VM and checks lsblk JSON output for crypto_LUKS
        filesystem type, confirming encryption survived the migration.

        Args:
            prepared_plan (dict[str, Any]): Test plan configuration with VM details.
            vm_ssh_connections (SSHConnectionManager): SSH connection manager.
            source_provider_data (dict[str, Any]): Provider configuration with guest credentials.

        Raises:
            AssertionError: If no LUKS-encrypted devices are found.
        """
        for vm in prepared_plan["virtual_machines"]:
            vm_name = vm["name"]
            source_vm_info = prepared_plan["source_vms_data"][vm_name]
            verify_luks_encryption(
                vm_name=vm_name,
                vm_ssh_connections=vm_ssh_connections,
                source_provider_data=source_provider_data,
                source_vm_info=source_vm_info,
            )

    def test_check_vms(
        self,
        prepared_plan: dict[str, Any],
        source_provider: "BaseProvider",
        destination_provider: "OCPProvider",
        source_provider_data: dict[str, Any],
        source_vms_namespace: str,
        source_provider_inventory: "ForkliftInventory",
        vm_ssh_connections: "SSHConnectionManager | None",
    ) -> None:
        """Validate migrated VMs.

        Args:
            prepared_plan (dict[str, Any]): Test plan configuration with VM details.
            source_provider (BaseProvider): Source provider connection.
            destination_provider (OCPProvider): Destination provider connection.
            source_provider_data (dict[str, Any]): Source provider configuration.
            source_vms_namespace (str): Source VMs namespace.
            source_provider_inventory (ForkliftInventory): Source provider inventory.
            vm_ssh_connections (SSHConnectionManager | None): SSH connections for connectivity testing.

        Raises:
            AssertionError: If any VM validation checks fail.
        """
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_data=source_provider_data,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )


@pytest.mark.vsphere
@pytest.mark.tier1
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_luks_cold_migration_wrong_key"])],
    indirect=True,
    ids=["luks-cold-wrong-key"],
)
@pytest.mark.usefixtures("cleanup_migrated_vms")
@pytest.mark.incremental
class TestLuksColdMigrationWrongKey(LuksColdMigrationBase):
    """Cold-migrating a LUKS-encrypted VM with a wrong passphrase must fail at the image conversion step.

    Purpose/Regression:
        Supplying an incorrect LUKS passphrase in the Plan's Secret must not
        produce a broken or unencrypted migrated VM: the migration must fail
        at the ImageConversion step, where the disk is decrypted during
        conversion. The chosen failure path is the wrong passphrase itself.

    Prerequisites:
        Register and connect a vSphere source provider and an OpenShift
        destination with MTV installed, with accessible destination storage
        and network. Use a disposable RHEL (or similar) VM with a
        LUKS-encrypted partition (the VM may be a clone so the original is
        untouched). Have an incorrect passphrase to supply in the Plan's
        Secret, and permission to create the Secret, maps, and a Plan in a
        target namespace.

    Test plan:
        1. Prepare the disposable VM, wait until it appears in Forklift
           inventory, and create a Kubernetes Secret holding an incorrect
           LUKS passphrase for the Plan to reference.
        2. Create a StorageMap for the VM's disks and a NetworkMap for its
           network. Confirm both are created.
        3. Create a cold Plan for the VM referencing the wrong-passphrase
           Secret. Check the Plan's Ready condition is True.
        4. Run the migration and check that it fails, with the VM's failed
           pipeline step reported as ImageConversion (the decryption
           failure). After the check, the failed migration resources,
           Secret, maps, and Plan are removed with the run.

    Expected result:
        The migration fails at the ImageConversion step rather than
        completing with an undecryptable or unencrypted disk. On failure of
        the expectation (migration succeeds or fails at another step),
        inspect the migration CR status for the VM's failed step and error,
        the conversion pod logs for the LUKS decryption error, and the
        passphrase Secret actually referenced by the Plan.
    """

    def test_migrate_vms(
        self,
        fixture_store: dict[str, Any],
        ocp_admin_client: "DynamicClient",
        target_namespace: str,
    ) -> None:
        """Execute migration — expects failure due to wrong LUKS passphrase.

        Args:
            fixture_store (dict[str, Any]): Resource tracking dictionary.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Target namespace for migration resources.
        """
        # ImageConversion is the pipeline step name in plan.instance.status — update if upstream renames it
        with pytest.raises(MigrationPlanExecError, match="ImageConversion"):
            execute_migration(
                fixture_store=fixture_store,
                ocp_admin_client=ocp_admin_client,
                plan=self.plan_resource,
                target_namespace=target_namespace,
            )
