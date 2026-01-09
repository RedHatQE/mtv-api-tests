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
    pass


class VmCloneError(Exception):
    pass


class MigrationNotFoundError(Exception):
    """Raised when Migration CR cannot be found for a Plan."""

    def __init__(self, plan_name: str) -> None:
        self.plan_name = plan_name

    def __str__(self) -> str:
        return f"Migration CR not found for Plan: {self.plan_name}"


class MigrationStatusError(Exception):
    """Raised when Migration CR has no status or incomplete status."""

    def __init__(self, migration_name: str) -> None:
        self.migration_name = migration_name

    def __str__(self) -> str:
        return f"Migration CR has no status or incomplete status: {self.migration_name}"


class VmPipelineError(Exception):
    """Raised when VM pipeline is missing or has no failed step."""

    def __init__(self, vm_name: str) -> None:
        self.vm_name = vm_name

    def __str__(self) -> str:
        return f"VM pipeline is missing or has no failed step: {self.vm_name}"
