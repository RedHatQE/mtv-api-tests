from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from ocp_resources.conversion import Conversion
from ocp_resources.secret import Secret
from pytest_testconfig import config as py_config

from exceptions.exceptions import ConversionError
from utilities.deep_inspection import (
    VDDK_IMAGE_NOT_SET_CONDITION,
    cancel_conversion,
    create_conversion_resource,
    verify_di_results,
    wait_for_conversion_complete,
    wait_for_conversion_phase,
    wait_for_conversion_pods,
    wait_for_conversion_pods_cleanup,
    wait_for_critical_conditions,
    wait_for_di_snapshot,
    wait_for_di_snapshot_cleanup,
)

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient
    from libs.providers.vmware import VMWareProvider


@pytest.mark.parametrize(
    "class_plan_config",
    [
        pytest.param(
            py_config["tests_params"]["test_standalone_di_vsphere"],
        ),
    ],
    indirect=True,
    ids=["standalone-di-vsphere"],
)
@pytest.mark.incremental
@pytest.mark.tier1
@pytest.mark.deep_inspection
@pytest.mark.vsphere
class TestStandaloneDeepInspection:
    """Run Deep Inspection on a vSphere VM through a standalone Conversion CR, without a migration Plan.

    Purpose/Regression:
        Deep Inspection must work standalone: creating a Conversion CR of
        type DeepInspection directly — with no Plan, StorageMap, or
        NetworkMap — must make the controller snapshot the VM, run the
        inspection pod, publish the inspection results, and clean up the
        snapshot. The pass signal is a Succeeded Conversion carrying OS,
        filesystem, and check results. There is no induced failure.

    Prerequisites:
        Register a vSphere source provider and an OpenShift destination with
        MTV installed. Prepare a connection Secret for the source VM
        (endpoint URL, credentials, and certificate fingerprint) and know
        the VDDK container image to use for the inspection pod. Use a
        disposable Linux VM whose disks can be snapshotted (snapshots are
        created and removed during inspection). Have permission to create a
        Conversion CR in a target namespace.

    Test plan:
        1. Prepare the source VM and confirm it is visible in Forklift
           inventory; note its inventory ID.
        2. Create a DeepInspection Conversion CR for the VM, referencing the
           connection Secret and the VDDK image. Confirm the CR is created.
        3. Wait for the inspection pipeline to complete and check the
           Conversion reaches Succeeded.
        4. Verify the inspection results on the Conversion CR: OS name and
           version, filesystem entries, and an allChecksPassed verdict are
           all present. The inspection snapshot is removed from the source
           VM as part of the pipeline, and the Conversion CR is removed
           with the run.

    Expected result:
        The Conversion succeeds with a populated inspectionResult (osInfo,
        filesystems, allChecksPassed). On failure, inspect the Conversion
        CR's phase and conditions and the inspection pod's logs in the
        target namespace.
    """

    conversion: Conversion

    def test_create_conversion(self, di_conversion_resource: Conversion) -> None:
        """Create a standalone DeepInspection Conversion CR."""
        self.__class__.conversion = di_conversion_resource
        assert self.conversion

    @pytest.mark.usefixtures("class_plan_config")
    def test_wait_for_completion(self) -> None:
        """Wait for the DeepInspection pipeline to complete."""
        wait_for_conversion_complete(
            conversion=self.conversion,
            timeout=py_config["plan_wait_timeout"],
        )

    def test_verify_di_results(self, di_vm_name: str) -> None:
        """Verify inspection results contain OS info and filesystem data."""
        verify_di_results(conversion=self.conversion, vm_name=di_vm_name)


@pytest.mark.parametrize(
    "class_plan_config",
    [
        pytest.param(
            py_config["tests_params"]["test_standalone_di_vsphere"],
        ),
    ],
    indirect=True,
    ids=["standalone-di-cancel-vsphere"],
)
@pytest.mark.incremental
@pytest.mark.tier1
@pytest.mark.deep_inspection
@pytest.mark.vsphere
class TestStandaloneDICancel:
    """Cancel a running standalone Deep Inspection, verify cleanup, and re-run it on the same VM.

    Purpose/Regression:
        A running Deep Inspection must be cancellable: setting the
        Conversion CR's status phase to Canceled must stop the inspection
        pod and remove the inspection snapshot from the source VM, and a new
        inspection of the same VM started afterwards must complete normally.
        The chosen failure path is the cancellation itself; the re-run proves
        no stale state blocks later inspections.

    Prerequisites:
        Register a vSphere source provider and an OpenShift destination with
        MTV installed. Prepare a connection Secret for the source VM
        (endpoint URL, credentials, and certificate fingerprint) and know
        the VDDK container image for the inspection pod. Use a disposable
        Linux VM whose disks can be snapshotted, and expect the inspection to
        run long enough to be caught in the Running phase. Have permission to
        create and patch Conversion CRs in a target namespace.

    Test plan:
        1. Prepare the source VM and confirm it is visible in Forklift
           inventory.
        2. Create a DeepInspection Conversion CR for the VM. Wait until it
           reaches the Running phase, the inspection snapshot exists on the
           source VM, and the inspection pod is running.
        3. Cancel the Conversion by setting its status phase to Canceled.
           Check that the Conversion ends in the Canceled phase (not
           Succeeded) and that the inspection pod is removed.
        4. Confirm the inspection snapshot is removed from the source VM
           after cancellation.
        5. Create a new DeepInspection Conversion CR for the same VM, wait
           for it to complete, and check it reaches Succeeded with valid
           inspection results (OS info, filesystems, allChecksPassed).
           The Conversion CRs are removed with the run.

    Expected result:
        The canceled Conversion stops cleanly — Canceled phase, no inspection
        pod, no leftover snapshot — and the re-run inspection succeeds with
        complete results. On failure, inspect the Conversion CRs' phases and
        conditions, the inspection pod's logs, and the source VM's snapshot
        list for a leftover inspection snapshot.
    """

    conversion: Conversion
    rerun_conversion: Conversion

    def test_create_conversion(self, di_conversion_resource: Conversion) -> None:
        """Create a standalone DeepInspection Conversion CR for cancel testing."""
        self.__class__.conversion = di_conversion_resource
        assert self.conversion

    @pytest.mark.usefixtures("class_plan_config")
    def test_wait_for_running(self) -> None:
        """Wait for the Conversion CR to reach Running phase."""
        wait_for_conversion_phase(
            conversion=self.conversion,
            phase=Conversion.Status.RUNNING,
            timeout=py_config["plan_wait_timeout"],
        )

    def test_cancel_conversion(self, di_vm_name: str, vmware_source_provider: "VMWareProvider") -> None:
        """Wait for DI pod to be running on the cluster, then cancel the conversion."""
        wait_for_di_snapshot(source_provider=vmware_source_provider, vm_name=di_vm_name)
        wait_for_conversion_pods(conversion=self.conversion)
        cancel_conversion(conversion=self.conversion)

    @pytest.mark.usefixtures("class_plan_config")
    def test_verify_cancel_cleanup(self) -> None:
        """Verify canceled conversion reaches Canceled phase and pod is cleaned up."""
        with pytest.raises(ConversionError, match="Canceled"):
            wait_for_conversion_complete(
                conversion=self.conversion,
                timeout=py_config["plan_wait_timeout"],
            )

        wait_for_conversion_pods_cleanup(conversion=self.conversion)

    def test_verify_snapshot_cleanup(self, di_vm_name: str, vmware_source_provider: "VMWareProvider") -> None:
        """Verify forklift DI snapshot is removed from the source VM after cancel."""
        wait_for_di_snapshot_cleanup(source_provider=vmware_source_provider, vm_name=di_vm_name)

    def test_rerun_di_after_cancel(
        self,
        di_resolved_vm: dict[str, Any],
        di_connection_secret: Secret,
        di_vddk_image: str,
        fixture_store: dict[str, Any],
        ocp_admin_client: "DynamicClient",
        target_namespace: str,
    ) -> None:
        """Create a new DI Conversion for the same VM after cancel."""
        self.__class__.rerun_conversion = create_conversion_resource(
            client=ocp_admin_client,
            fixture_store=fixture_store,
            connection_secret=di_connection_secret,
            vm_id=di_resolved_vm["id"],
            vm_name=di_resolved_vm["name"],
            vddk_image=di_vddk_image,
            target_namespace=target_namespace,
        )
        assert self.rerun_conversion

    @pytest.mark.usefixtures("class_plan_config")
    def test_rerun_wait_for_completion(self) -> None:
        """Wait for the re-run DI to complete successfully."""
        wait_for_conversion_complete(
            conversion=self.rerun_conversion,
            timeout=py_config["plan_wait_timeout"],
        )

    def test_rerun_verify_results(self, di_vm_name: str) -> None:
        """Verify the re-run DI produced valid inspection results."""
        verify_di_results(conversion=self.rerun_conversion, vm_name=di_vm_name)


@pytest.mark.parametrize(
    "class_plan_config",
    [
        pytest.param(
            py_config["tests_params"]["test_standalone_di_vsphere"],
        ),
    ],
    indirect=True,
    ids=["standalone-di-validation-vsphere"],
)
@pytest.mark.incremental
@pytest.mark.tier1
@pytest.mark.deep_inspection
@pytest.mark.vsphere
class TestStandaloneDIValidation:
    """A DeepInspection Conversion missing its VDDK image must be rejected with a Critical condition.

    Purpose/Regression:
        Creating a DeepInspection Conversion CR without the required
        vddkImage field must not run the inspection: the controller must set
        a Critical condition (VDDKImageNotSet) and keep the Conversion out of
        Running and Succeeded phases. The failure path is the missing field
        itself — validation must block the pipeline.

    Prerequisites:
        Register a vSphere source provider and an OpenShift destination with
        MTV installed. Prepare a connection Secret for the source VM
        (endpoint URL, credentials, and certificate fingerprint). Use a
        disposable Linux VM visible in Forklift inventory. Have permission
        to create a Conversion CR in a target namespace.

    Test plan:
        1. Prepare the source VM and confirm it is visible in Forklift
           inventory.
        2. Create a DeepInspection Conversion CR for the VM that omits the
           vddkImage field. Confirm the CR is created.
        3. Wait for the controller's validation and check that the
           Conversion has a Critical VDDKImageNotSet condition and that its
           phase, sampled when the Critical condition appears, is neither
           Running nor Succeeded. The Conversion CR is removed with the
           run.

    Expected result:
        The Conversion is blocked with a Critical VDDKImageNotSet condition
        and has not reached Running or Succeeded at that point. On failure,
        inspect the Conversion CR's conditions and phase and the forklift
        controller logs for the validation decision.
    """

    conversion: Conversion

    def test_create_invalid_conversion(self, di_invalid_conversion_resource: Conversion) -> None:
        """Create a DeepInspection Conversion CR without vddkImage."""
        self.__class__.conversion = di_invalid_conversion_resource
        assert self.conversion

    @pytest.mark.usefixtures("class_plan_config")
    def test_verify_validation_error(self) -> None:
        """Verify controller sets Critical condition for missing vddkImage."""
        critical_conditions, phase = wait_for_critical_conditions(
            conversion=self.conversion,
            timeout=py_config["plan_wait_timeout"],
        )

        condition_types = [c.get("type") for c in critical_conditions]
        assert VDDK_IMAGE_NOT_SET_CONDITION in condition_types, (
            f"Conversion '{self.conversion.name}' expected {VDDK_IMAGE_NOT_SET_CONDITION} condition, "
            f"got: {condition_types}"
        )

        assert phase not in {Conversion.Status.RUNNING, Conversion.Status.SUCCEEDED}, (
            f"Conversion '{self.conversion.name}' should not reach Running or Succeeded with validation errors, "
            f"got phase '{phase}'"
        )
