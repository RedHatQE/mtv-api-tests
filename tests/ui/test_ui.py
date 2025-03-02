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
def test_basic_elements(request: pytest.FixtureRequest, console_page: Page) -> None:
    try:
        # Check the Migration side tab is visible
        expect(console_page.get_by_test_id("migration-nav-item")).to_be_visible(timeout=20_000)

        # Click on Migration tab
        console_page.get_by_test_id("migration-nav-item").click()

        # Check that Migration all sub tabs is visible
        # expect(page.get_by_role("link", name="Overview")).to_be_visible(timeout=10_000)
        expect(console_page.get_by_test_id("providers-nav-item")).to_be_visible(timeout=10_000)
        expect(console_page.get_by_test_id("plans-nav-item")).to_be_visible(timeout=10_000)
        expect(console_page.get_by_test_id("network-mappings-nav-item")).to_be_visible(timeout=10_000)
        expect(console_page.get_by_test_id("network-mappings-nav-item")).to_be_visible(timeout=10_000)

    except Exception:
        if not request.node.config.getoption("skip_data_collector"):
            console_page.screenshot(
                path=f"{request.node.config.getoption('data_collector_path')}/{request.node.name}/screenshot.png",
                full_page=True,
            )
        raise
