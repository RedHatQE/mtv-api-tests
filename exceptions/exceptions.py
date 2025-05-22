class RemoteClusterAndLocalCluterNamesError(Exception):
    pass


class ForkliftPodsNotRunningError(Exception):
    pass


class VmMissingVmxError(Exception):
    def __init__(self, vms: list[str]) -> None:
        self.vms = vms

    def __str__(self) -> str:
        return f"Some VMs are missing VMX file: {self.vms}"


class VmBadDatastoreError(Exception):
    def __init__(self, vms: list[str]) -> None:
        self.vms = vms

    def __str__(self) -> str:
        return f"Some VMs have bad datastore status: {self.vms}"


class NoVmsFoundError(Exception):
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
