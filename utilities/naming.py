import re
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
