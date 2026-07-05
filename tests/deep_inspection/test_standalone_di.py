from __future__ import annotations

import pytest
from ocp_resources.conversion import Conversion
from pytest_testconfig import config as py_config

from utilities.deep_inspection import verify_di_results, wait_for_conversion_complete


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
    """Standalone Deep Inspection without a migration plan.

    Creates a DeepInspection Conversion CR directly (no Plan, StorageMap,
    or NetworkMap). The controller creates a snapshot, runs the inspection
    pod, fetches results, and cleans up the snapshot automatically.

    No cleanup_migrated_vms: DI does not create migrated VMs.
    """

    conversion: Conversion

    def test_create_conversion(self, di_conversion_resource: Conversion) -> None:
        """Create a standalone DeepInspection Conversion CR."""
        self.__class__.conversion = di_conversion_resource
        assert self.conversion

    @pytest.mark.usefixtures("di_vm_name")
    def test_wait_for_completion(self) -> None:
        """Wait for the DeepInspection pipeline to complete."""
        wait_for_conversion_complete(
            conversion=self.conversion,
            timeout=py_config["plan_wait_timeout"],
        )

    def test_verify_di_results(self, di_vm_name: str) -> None:
        """Verify inspection results contain OS info and filesystem data."""
        verify_di_results(conversion=self.conversion, vm_name=di_vm_name)
