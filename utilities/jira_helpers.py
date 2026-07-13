import pytest
import requests
from pytest_jira import CONNECTION_ERROR_FLAG_NAME, STRICT
from simple_logger.logger import get_logger

LOGGER = get_logger(__name__)

JIRA_PLUGIN_NAME = "jira_plugin"


def is_jira_issue_open(request: pytest.FixtureRequest, issue_id: str) -> bool | None:
    """Check if a Jira issue is open via pytest-jira plugin.

    Args:
        request: Pytest fixture request
        issue_id: Jira issue key (e.g. MTV-6072)

    Returns:
        True if open, False if resolved, None if Jira unavailable

    Raises:
        requests.RequestException: If connection fails and strategy is STRICT
    """
    jira_plugin = request.config.pluginmanager.getplugin(JIRA_PLUGIN_NAME)
    if jira_plugin:
        try:
            return not jira_plugin.is_issue_resolved(issue_id)
        except requests.RequestException as e:
            strategy = request.config.getoption(CONNECTION_ERROR_FLAG_NAME)
            if strategy == STRICT:
                raise
            else:
                LOGGER.warning(
                    f"Jira connection failed for issue '{issue_id}' (strategy={strategy!r}); "
                    f"treating as unavailable: {e}"
                )
    else:
        LOGGER.warning(f"Jira plugin '{JIRA_PLUGIN_NAME}' not found; treating issue '{issue_id}' as unavailable")
    return None
