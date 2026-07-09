import time

from ocp_resources.provider import Provider
from ocp_resources.resource import ResourceEditor
from simple_logger.logger import get_logger

LOGGER = get_logger(__name__)

_INVENTORY_REFRESH_READY_TIMEOUT = 180


def force_inventory_refresh(provider: Provider) -> None:
    """Force Forklift provider inventory refresh by patching spec.settings._refresh.

    Args:
        provider (Provider): Forklift Provider resource to refresh.

    Raises:
        TimeoutExpiredError: If the provider does not become Ready within the timeout.
    """
    refresh_timestamp = str(int(time.time()))
    LOGGER.info(f"Forcing inventory refresh for provider '{provider.name}' with _refresh={refresh_timestamp}")
    patch = {"spec": {"settings": {"_refresh": refresh_timestamp}}}
    ResourceEditor(patches={provider: patch}).update()
    provider.wait_for_condition(condition="Ready", status="True", timeout=_INVENTORY_REFRESH_READY_TIMEOUT)
