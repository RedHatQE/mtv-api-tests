import pytest
import requests
from pytest_jira import CONNECTION_ERROR_FLAG_NAME, SKIP, STRICT
from simple_logger.logger import get_logger

LOGGER = get_logger(__name__)

JIRA_PLUGIN_NAME = "jira_plugin"


def is_jira_issue_open(request: pytest.FixtureRequest, issue_id: str) -> bool | None:
    """Check if a Jira issue is open, mirroring pytest-jira's jira_issue fixture.

    Args:
        request: Pytest fixture request for plugin/config access
        issue_id: Jira issue key (e.g. MTV-6072)

    Returns:
        True if issue is open, False if resolved, None if Jira is unavailable

    Raises:
        requests.RequestException: If Jira connection fails and connection strategy is STRICT
    """
    jira_plugin = request.config.pluginmanager.getplugin(JIRA_PLUGIN_NAME)
    if jira_plugin:
        try:
            return not jira_plugin.is_issue_resolved(issue_id)
        except requests.RequestException as e:
            strategy = request.config.getoption(CONNECTION_ERROR_FLAG_NAME)
            if strategy == SKIP:
                LOGGER.warning(f"Jira connection failed for issue '{issue_id}'; treating as unavailable: {e}")
                return None
            elif strategy == STRICT:
                raise
    LOGGER.warning(f"Jira plugin '{JIRA_PLUGIN_NAME}' not found; treating issue '{issue_id}' as unavailable")
    return None
