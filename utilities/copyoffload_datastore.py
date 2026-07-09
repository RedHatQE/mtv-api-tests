from typing import Any

SYMBOLIC_SECONDARY_DATASTORE = "secondary_datastore_id"
SYMBOLIC_NON_XCOPY_DATASTORE = "non_xcopy_datastore_id"

ERR_SECONDARY_DS_NOT_CONFIGURED = (
    "Disk requested secondary datastore but copyoffload.secondary_datastore_id is not configured"
)
ERR_NON_XCOPY_DS_NOT_CONFIGURED = (
    "Disk requested non-XCOPY datastore but copyoffload.non_xcopy_datastore_id is not configured"
)


def resolve_datastore_moid_from_disk_config(disk_datastore_id: str, copyoffload_config: dict[str, Any]) -> str:
    """Resolve a disk datastore_id to a vSphere MoID.

    Symbolic keys (``secondary_datastore_id``, ``non_xcopy_datastore_id``) are
    mapped to MoIDs from ``copyoffload_config``; literal values are returned as-is.

    Args:
        disk_datastore_id: Datastore ID from disk config (symbolic key or MoID)
        copyoffload_config: copyoffload section from source provider data

    Returns:
        Resolved vSphere datastore MoID

    Raises:
        ValueError: If a symbolic datastore key cannot be resolved from copyoffload config
    """
    if disk_datastore_id == SYMBOLIC_SECONDARY_DATASTORE:
        resolved_id = copyoffload_config.get("secondary_datastore_id")
        if not resolved_id:
            raise ValueError(ERR_SECONDARY_DS_NOT_CONFIGURED)
        return resolved_id

    if disk_datastore_id == SYMBOLIC_NON_XCOPY_DATASTORE:
        resolved_id = copyoffload_config.get("non_xcopy_datastore_id")
        if not resolved_id:
            raise ValueError(ERR_NON_XCOPY_DS_NOT_CONFIGURED)
        return resolved_id

    return disk_datastore_id
