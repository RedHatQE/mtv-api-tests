from typing import Any

SYMBOLIC_SECONDARY_DATASTORE = "secondary_datastore_id"
SYMBOLIC_NON_XCOPY_DATASTORE = "non_xcopy_datastore_id"

ERR_SECONDARY_DS_NOT_CONFIGURED = (
    "Disk requested secondary datastore but copyoffload.secondary_datastore_id is not configured"
)
ERR_NON_XCOPY_DS_NOT_CONFIGURED = (
    "Disk requested non-XCOPY datastore but copyoffload.non_xcopy_datastore_id is not configured"
)
ERR_EMPTY_DISK_DATASTORE_ID = "Disk datastore_id is empty. Provide a valid MoID or omit the field."


def format_custom_datastore_not_found_message(moid: str) -> str:
    """Format error message when a custom datastore MoID is not found in vSphere.

    Args:
        moid: vSphere datastore MoID that could not be resolved

    Returns:
        Error message describing the missing or inaccessible datastore
    """
    return f"Custom datastore not found for disk. MoID '{moid}' is invalid or not accessible."


def _configured_datastore_moid(copyoffload_config: dict[str, Any], key: str) -> str:
    """Resolve a configured datastore MoID from copyoffload config.

    Args:
        copyoffload_config: copyoffload section from source provider data
        key: Config key to resolve (e.g. secondary_datastore_id)

    Returns:
        Resolved vSphere datastore MoID

    Raises:
        ValueError: If the key is missing or the value is not a non-empty string
    """
    resolved_id = copyoffload_config.get(key)
    if not resolved_id:
        if key == SYMBOLIC_SECONDARY_DATASTORE:
            raise ValueError(ERR_SECONDARY_DS_NOT_CONFIGURED)
        if key == SYMBOLIC_NON_XCOPY_DATASTORE:
            raise ValueError(ERR_NON_XCOPY_DS_NOT_CONFIGURED)
        # Defensive: current callers only pass symbolic keys, but guard against future callers.
        raise ValueError(f"copyoffload.{key} is not configured")
    if not isinstance(resolved_id, str):
        raise ValueError(f"copyoffload.{key} must be a string, got {type(resolved_id).__name__}")
    return resolved_id


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
        return _configured_datastore_moid(copyoffload_config, SYMBOLIC_SECONDARY_DATASTORE)

    if disk_datastore_id == SYMBOLIC_NON_XCOPY_DATASTORE:
        return _configured_datastore_moid(copyoffload_config, SYMBOLIC_NON_XCOPY_DATASTORE)

    return disk_datastore_id
