import re
from typing import Any

import shortuuid

from exceptions.exceptions import InvalidVMNameError

# Compiled regex patterns for performance (reused in sanitize_kubernetes_name)
_INVALID_CHARS_PATTERN = re.compile(r"[^a-z0-9-]+")
_LEADING_TRAILING_PATTERN = re.compile(r"^[^a-z0-9]+|[^a-z0-9]+$")


def generate_name_with_uuid(name: str) -> str:
    _name = f"{name}-{shortuuid.ShortUUID().random(length=4).lower()}"
    _name = _name.replace("_", "-").replace(".", "-").lower()
    return _name


def sanitize_kubernetes_name(name: str, max_length: int = 63) -> str:
    """Sanitize a VM name to comply with Kubernetes DNS-1123 naming conventions.

    Rules:
    - lowercase alphanumeric characters and '-'
    - must start and end with an alphanumeric character
    - max `max_length` characters

    Args:
        name (str): The VM name to sanitize
        max_length (int): Maximum length for the sanitized name. Defaults to 63.

    Returns:
        str: The sanitized name compliant with Kubernetes DNS-1123 conventions

    Raises:
        InvalidVMNameError: If the name cannot be sanitized to a valid DNS-1123 name
            (i.e., contains no alphanumeric characters)
    """
    sanitized = name.replace("_", "-").replace(".", "-").lower()
    sanitized = _INVALID_CHARS_PATTERN.sub("-", sanitized)
    sanitized = _LEADING_TRAILING_PATTERN.sub("", sanitized)
    sanitized = sanitized[:max_length].rstrip("-")
    if not sanitized:
        raise InvalidVMNameError(
            f"VM name '{name}' cannot be sanitized to a valid DNS-1123 name. "
            "The name must contain at least one alphanumeric character."
        )
    return sanitized


def resolve_destination_vm_name(vm: dict[str, Any]) -> str:
    """Resolve the expected Kubernetes destination VM name.

    Uses the explicit targetName if set, otherwise predicts the name Forklift
    will produce by sanitizing the source VM name to DNS-1123 conventions.

    Args:
        vm (dict[str, Any]): VM configuration dict from prepared_plan["virtual_machines"].

    Returns:
        str: The resolved destination VM name.

    Raises:
        InvalidVMNameError: If the VM name cannot be sanitized to a valid DNS-1123 name.
        KeyError: If the VM dict does not contain a "name" key.
    """
    return vm.get("targetName") or sanitize_kubernetes_name(vm["name"])


def sanitize_test_name_for_path(test_name: str) -> str:
    """Sanitize a pytest test name for safe use in file paths.

    Replaces characters that cause issues in file paths or shell commands:
    - Brackets [ ] → underscores _
    - Colons : → hyphens -
    - Other special characters → hyphens -

    Args:
        test_name (str): The pytest test name (may include parametrization brackets)

    Returns:
        str: The sanitized test name safe for use in file paths

    Example:
        >>> sanitize_test_name_for_path("test_migrate_vms[MTV-565:copyoffload-mixed-datastore]")
        'test_migrate_vms_MTV-565-copyoffload-mixed-datastore_'
    """
    # Replace brackets with underscores
    sanitized = test_name.replace("[", "_").replace("]", "_")
    # Replace colons with hyphens
    sanitized = sanitized.replace(":", "-")
    # Replace any other special characters with hyphens (optional, for extra safety)
    sanitized = re.sub(r"[^\w\-]", "-", sanitized)
    return sanitized
