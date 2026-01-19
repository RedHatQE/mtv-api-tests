import re
import shortuuid


class InvalidVMNameError(Exception):
    """
    Exception raised when a VM name cannot be sanitized to a valid Kubernetes DNS-1123 name.

    This error occurs when the sanitization process results in an empty string,
    typically because the input name contains only invalid characters that get
    stripped during sanitization.
    """

    pass


def generate_name_with_uuid(name: str) -> str:
    _name = f"{name}-{shortuuid.ShortUUID().random(length=4).lower()}"
    _name = _name.replace("_", "-").replace(".", "-").lower()
    return _name


def sanitize_kubernetes_name(name: str) -> str:
    """
    Sanitize a VM name to comply with Kubernetes DNS-1123 subdomain naming conventions.

    Rules:
    - lowercase alphanumeric characters and '-'
    - must start and end with an alphanumeric character
    - max 253 characters

    This matches how the MTV operator converts source VM names to valid Kubernetes resource names,
    and is consistent with generate_name_with_uuid() behavior.

    Args:
        name: The original VM name (may contain capitals, underscores, periods, etc.)

    Returns:
        A Kubernetes-compliant name (lowercase, underscores and periods replaced with hyphens)

    Raises:
        InvalidVMNameError: If the name cannot be sanitized to a valid DNS-1123 name
                           (e.g., contains only invalid characters)

    Example:
        >>> sanitize_kubernetes_name("auto-8ysl-XCopy_Test_VM_CAPS-wnpn")
        'auto-8ysl-xcopy-test-vm-caps-wnpn'
        >>> sanitize_kubernetes_name("vm.with.periods")
        'vm-with-periods'
    """
    # Lowercase and replace underscores and periods with hyphens (consistent with generate_name_with_uuid)
    sanitized = name.replace("_", "-").replace(".", "-").lower()
    # Collapse any other invalid characters to hyphens
    sanitized = re.sub(r"[^a-z0-9-]+", "-", sanitized)
    # Must start/end with alphanumeric
    sanitized = re.sub(r"^[^a-z0-9]+|[^a-z0-9]+$", "", sanitized)
    # Enforce max length (DNS-1123 subdomain)
    sanitized = sanitized[:253].rstrip("-")
    if not sanitized:
        raise InvalidVMNameError(
            f"VM name '{name}' cannot be sanitized to a valid DNS-1123 name. "
            "The name must contain at least one alphanumeric character."
        )
    return sanitized
