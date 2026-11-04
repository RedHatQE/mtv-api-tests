class RemoteClusterAndLocalCluterNamesError(Exception):
    pass


class ForkliftPodsNotRunningError(Exception):
    pass


class MtvOperatorNotInstalledError(Exception):
    """Raised when the MTV operator Subscription is not found in the expected namespace."""

    def __init__(self, namespace: str) -> None:
        """Initialize MtvOperatorNotInstalledError.

        Args:
            namespace (str): The namespace where the MTV operator Subscription was expected.
        """
        self.namespace = namespace
        super().__init__(
            f"MTV operator Subscription not found in namespace '{namespace}'. "
            f"Ensure MTV is installed or set the correct namespace with --tc=mtv_namespace:<namespace>"
        )


class VmMissingVmxError(Exception):
    def __init__(self, vm: str) -> None:
        self.vm = vm

    def __str__(self) -> str:
        return f"VM is missing VMX file: {self.vm}"


class VmBadDatastoreError(Exception):
    def __init__(self, vm: str) -> None:
        self.vm = vm

    def __str__(self) -> str:
        return f"VM have bad datastore status: {self.vm}"


class VmNotFoundError(Exception):
    pass


class MigrationPlanExecError(Exception):
    pass


class MigrationVmUnschedulableError(Exception):
    """Raised when a migrated VM cannot be scheduled on any destination node.

    The scheduler condition is terminal for a test run: the suite never frees destination node
    capacity, so waiting for the plan timeout only delays a certain failure.
    """

    def __init__(self, vm_name: str, scheduler_message: str) -> None:
        """Initialize MigrationVmUnschedulableError.

        Args:
            vm_name (str): Name of the destination VM the scheduler rejected.
            scheduler_message (str): Raw message from the PodScheduled Unschedulable condition.
        """
        self.vm_name = vm_name
        self.scheduler_message = scheduler_message
        super().__init__(f"Migrated VM '{vm_name}' is unschedulable on the destination cluster: {scheduler_message}")


class SessionTeardownError(Exception):
    pass


class ResourceNameNotStartedWithSessionUUIDError(Exception):
    pass


class OvirtMTVDatacenterNotFoundError(Exception):
    pass


class OvirtMTVDatacenterStatusError(Exception):
    pass


class MissingProvidersFileError(Exception):
    def __init__(self, path: str = ".providers.json") -> None:
        """Initialize MissingProvidersFileError.

        Args:
            path (str): Path to the providers JSON file.
        """
        super().__init__(f"No provider configurations found in '{path}'")


class ProviderEmptyContentError(Exception):
    def __init__(self, path: str) -> None:
        """Initialize ProviderEmptyContentError.

        Args:
            path (str): Path to the empty providers JSON file.
        """
        super().__init__(f"Providers JSON file is empty: '{path}'")


class VmCloneError(Exception):
    pass


class MigrationNotFoundError(Exception):
    """Raised when Migration CR cannot be found for a Plan."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class MigrationStatusError(Exception):
    """Raised when Migration CR has no status or incomplete status."""

    def __init__(self, migration_name: str) -> None:
        self.migration_name = migration_name
        super().__init__(f"Migration CR '{migration_name}' has no status or incomplete status")


class VmPipelineError(Exception):
    """Raised when VM pipeline is missing or has no failed step."""

    def __init__(self, vm_name: str) -> None:
        self.vm_name = vm_name
        super().__init__(f"VM '{vm_name}' pipeline is missing or has no failed step")


class VmMigrationStepMismatchError(Exception):
    """Raised when VMs in the same plan fail at different migration steps."""

    def __init__(self, plan_name: str, failed_steps: dict[str, str]) -> None:
        self.plan_name = plan_name
        self.failed_steps = failed_steps
        super().__init__(f"VMs in plan '{plan_name}' failed at different steps: {failed_steps}")


class InvalidVMNameError(Exception):
    pass


class GuestCommandError(Exception):
    """Raised when a command executed via VMware Guest Operations exits with non-zero code."""


class MustGatherImageError(ValueError):
    """Raised when installed MTV image metadata cannot resolve a must-gather image."""


class MtvUpgradeError(Exception):
    """Raised when MTV operator upgrade fails."""


class SSHConnectionSetupError(Exception):
    """Raised when SSH connection setup fails (e.g., missing virtctl, port-forward failure)."""


class PowerShellCommandError(Exception):
    """Raised when a PowerShell cmdlet or script executed via PSRP fails."""


class StorageProvisioningError(Exception):
    """Raised when destination storage cannot provision volumes (e.g. broken Ceph)."""

    def __init__(self, storage_class: str, pvc_name: str, detail: str) -> None:
        """Initialize StorageProvisioningError.

        Args:
            storage_class (str): Storage class that failed to provision a volume.
            pvc_name (str): Name of the PVC that could not be provisioned.
            detail (str): What was observed on the PVC (e.g. how long it stayed Pending).
        """
        self.storage_class = storage_class
        self.pvc_name = pvc_name
        self.detail = detail
        super().__init__(f"Storage class '{storage_class}' cannot provision volumes: PVC '{pvc_name}' {detail}")


class ConversionError(Exception):
    """Raised when a Conversion CR reaches Failed or Canceled phase."""

    def __init__(self, conversion_name: str, phase: str, message: str = "") -> None:
        """Initialize ConversionError.

        Args:
            conversion_name (str): Name of the Conversion CR.
            phase (str): The terminal phase (Failed or Canceled).
            message (str): Additional context about the failure.
        """
        self.conversion_name = conversion_name
        self.phase = phase
        detail = f": {message}" if message else ""
        super().__init__(f"Conversion '{conversion_name}' reached phase '{phase}'{detail}")


class ClusterStateProbeTimeout(Exception):
    """Raised internally when a cluster-state probe did not finish inside the snapshot budget.

    The probe runs on a daemon thread and cannot be cancelled; this signals the snapshot to stop
    waiting so the failure it is documenting is reported on time.
    """
