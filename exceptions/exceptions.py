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


class MigrationPlanExecStopError(Exception):
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
