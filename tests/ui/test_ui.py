import os

import pytest
from kubernetes.dynamic.client import DynamicClient
from ocp_resources.console_config_openshift_io import Console
from playwright.sync_api import Page, expect


@pytest.fixture(scope="session")
def cluster_data(ocp_admin_client: DynamicClient) -> tuple[str, str, str]:
    console_url = Console(client=ocp_admin_client, name="cluster").instance.status.consoleURL
    username = os.environ.get("CLUSTER_USERNAME")
    password = os.environ.get("CLUSTER_PASSWORD")
    if not username or not password:
        raise ValueError("CLUSTER_USERNAME and CLUSTER_PASSWORD must be set as environment variables")

    return username, password, console_url


@pytest.mark.ui
@pytest.mark.parametrize(
    "test_id",
    [
        "migration-nav-item",
        "overview",
        "providers-nav-item",
        "plans-nav-item",
        "network-mappings-nav-item",
        "storage-mappings-nav-item",
    ],
    ids=[
        "Migration-tab",
        "Overview-sub-tab",
        "Providers-sub-tab",
        "Plans-sub-tab",
        "NetworkMap-sub-tab",
        "StorageMap-sub-tab",
    ],
)
def test_basic_elements_is_visible(request: pytest.FixtureRequest, console_page: Page, test_id: str) -> None:
    try:
        expect(console_page.get_by_test_id(test_id)).to_be_visible(timeout=20_000)

        if test_id == "migration-nav-item":
            # Click on Migration tab
            console_page.get_by_test_id(test_id).click()

    except Exception:
        if not request.node.config.getoption("skip_data_collector"):
            console_page.screenshot(
                path=f"{request.node.config.getoption('data_collector_path')}/{request.node.name}/screenshot.png",
                full_page=True,
            )
        raise
