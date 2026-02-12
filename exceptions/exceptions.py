class RemoteClusterAndLocalCluterNamesError(Exception):
    pass


class ForkliftPodsNotRunningError(Exception):
    pass


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


class SessionTeardownError(Exception):
    pass


class ResourceNameNotStartedWithSessionUUIDError(Exception):
    pass


class OvirtMTVDatacenterNotFoundError(Exception):
    pass


class OvirtMTVDatacenterStatusError(Exception):
    pass


class MissingProvidersFileError(Exception):
    def __init__(self) -> None:
        super().__init__("'.providers.json' file is missing or empty")


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

    def __init__(self, plan_name: str, failed_steps: dict[str, str | None]) -> None:
        self.plan_name = plan_name
        self.failed_steps = failed_steps
        super().__init__(f"VMs in plan '{plan_name}' failed at different steps: {failed_steps}")


class InvalidVMNameError(Exception):
    pass
