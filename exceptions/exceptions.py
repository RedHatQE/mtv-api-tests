class RemoteClusterAndLocalCluterNamesError(Exception):
    pass


class ForkliftPodsNotRunningError(Exception):
    pass


class VmMissingVmxError(Exception):
    pass


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
