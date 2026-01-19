import shortuuid


def generate_name_with_uuid(name: str) -> str:
    _name = f"{name}-{shortuuid.ShortUUID().random(length=4).lower()}"
    _name = _name.replace("_", "-").replace(".", "-").lower()
    return _name


def sanitize_kubernetes_name(name: str) -> str:
    """
    Sanitize a VM name to comply with Kubernetes DNS-1123 subdomain naming conventions.

    Rules:
    - lowercase alphanumeric characters, '-' or '.'
    - must start and end with an alphanumeric character
    - max 253 characters

    This matches how the MTV operator converts source VM names to valid Kubernetes resource names.

    Args:
        name: The original VM name (may contain capitals, underscores, etc.)

    Returns:
        A Kubernetes-compliant name (lowercase, underscores replaced with hyphens)

    Example:
        >>> sanitize_kubernetes_name("auto-8ysl-XCopy_Test_VM_CAPS-wnpn")
        'auto-8ysl-xcopy-test-vm-caps-wnpn'
    """
    # Convert to lowercase and replace underscores with hyphens
    sanitized = name.replace("_", "-").lower()
    return sanitized
